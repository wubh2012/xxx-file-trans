"""识别流水线：几何自举（ADR-0001）→ 规范画布采样 → 帧头解析 → 交叉校验。

无默认参数（docs/protocol.md §5 冻结）：BIT 由角标白块边长 ÷ 3 推得，
COLS / ROWS 由四角标定出的数据网格矩形推得（角标内角 = 数据网格外角，
与画布外沿无关）；再与帧头 GEO / COLS / ROWS 交叉校验，不一致整帧丢弃。
PAD 不再由画布边沿推导（issue #22）：角标只锚定数据网格，画布黑边在
裁切采集下不可见，PAD 在几何上不可测——以帧头声明为准（CRC 保证帧头
完整性），接收端由此容忍采集画面的任意裁切边距。

发送端几何约定（sender.html 头注声明为实现基准，接收端据此自举）：
静默区黑色宽 PAD×BIT；角标 = 白色实心方块、边长严格 3×BIT、内角与
数据网格四角外角重合（对角线方向伸入静默区，故 PAD ≥ 3）；黑块 = 1。
早期版本 PAD 推导假设「画面即画布、静默区贴边」（ADR-0001 追记），
现升级为裁切容忍；camera #13 的透视/裁切校正在此扩展。
"""

from dataclasses import dataclass
from functools import lru_cache
from typing import NamedTuple

import cv2
import numpy as np

from receiver.metadata import FIXED_BYTES as METADATA_FIXED_BYTES
from receiver.metadata import FileMetadata, parse_metadata
from receiver.protocol import (
    FLAGS_ALLOWED,
    FLAGS_FEC,
    FLAGS_FOUNTAIN,
    FLAGS_REPAIR,
    FRAME_NO_METADATA,
    HEADER_BYTES,
    SYNC,
    VER,
    VER_FOUNTAIN,
    FrameRejected,
    fec_parity_count,
    is_fec_frame,
    verify_crc,
)

# 角标几何一致性约束的容差：PNG / desktop 源像素精确，camera 透视校正
# 将来另立 ADR（issue #37 设计）在此处扩展。
_SIDE_EQUAL_TOL = 1  # 同边候选对齐允许的测量偏差（像素）
_MAX_GRID_DIM = 255  # COLS / ROWS 帧头各 1 字节（§2），剪枝上限


@dataclass
class FrameHeader:
    file_id: int
    frame_no: int
    total_frames: int
    data_len: int
    chunk_size: int
    cols: int
    rows: int
    bit: int
    pad: int
    flags: int


@dataclass
class DecodedFrame:
    header: FrameHeader
    payload: bytes  # 数据区有效字节（DATA_LEN 截断，补位不计）
    metadata: FileMetadata | None = None  # 仅元数据帧非 None（§4）


@dataclass
class MeasuredGeometry:
    """几何自举测量结果（bit/cols/rows/side 来自画面，无默认值）。

    pad 为原始测量值（网格原点 ÷ BIT），仅在「画面即画布」时等于真实
    PAD；裁切输入下它混入裁切偏移，仅供 calibrate 探针残差参考，
    解码路径的 PAD 由帧头交叉校验裁定（见 decode_frame）。
    """

    bit: int
    pad: int
    cols: int
    rows: int
    side: int  # 角标边长实测值（px；calibrate 探针用于边长残差统计，#11）
    # 播放器缩放时，逻辑 BIT 仍取帧头值，显示格距由角标矩形标定。
    pitch_x: float | None = None
    pitch_y: float | None = None


def parse_header(header: bytes) -> FrameHeader:
    """解析 26 字节帧头（§2）。SYNC / VER 不符整帧丢弃。"""
    sync = int.from_bytes(header[0:2], "big")
    if sync != SYNC:
        raise FrameRejected("sync", f"期望 0x{SYNC:04X}，实际 0x{sync:04X}")
    if header[2] not in (VER, VER_FOUNTAIN):
        raise FrameRejected("ver", f"期望 0x{VER:02X}，实际 0x{header[2]:02X}")
    flags = int.from_bytes(header[20:22], "big")
    if header[2] == VER and flags & (FLAGS_FOUNTAIN | FLAGS_REPAIR):
        raise FrameRejected("flags", "v1 不接受喷泉码标志")
    if header[2] == VER_FOUNTAIN and (not flags & FLAGS_FOUNTAIN or flags & FLAGS_FEC):
        raise FrameRejected("flags", "v2 必须标记喷泉码且不能混用固定 FEC")
    return FrameHeader(
        file_id=int.from_bytes(header[3:7], "big"),
        frame_no=int.from_bytes(header[7:10], "big"),
        total_frames=int.from_bytes(header[10:13], "big"),
        data_len=int.from_bytes(header[13:15], "big"),
        chunk_size=int.from_bytes(header[15:17], "big"),
        cols=header[17],
        rows=header[18],
        bit=header[19] >> 4,
        pad=header[19] & 0x0F,
        flags=int.from_bytes(header[20:22], "big"),
    )


def _solid_square_candidates(bw: np.ndarray) -> list[tuple[int, int, int, int]]:
    """白 pixel 连通域（4 连通）中「实心正方形」候选：(left, top, side, side)。

    实心 = 像素数与外接框面积相等；边长被 3 整除（角标 = 3×BIT，BIT 为整数）。
    4 连通保证角标（与数据网格仅角点相触）不与网格白块粘连。
    """
    n, _, stats, _ = cv2.connectedComponentsWithStats(bw, connectivity=4)
    out = []
    for i in range(1, n):  # 0 号是背景
        left, top, w, h, area = stats[i]
        if w == h and area == w * h and w % 3 == 0:
            out.append((int(left), int(top), w, h))
    return out


class _Quad(NamedTuple):
    """候选四元组裁决结果（area 用于「外沿者即真解」比较，issue #37）。"""

    area: int
    side: int
    rect: tuple[int, int, int, int]  # 网格矩形 x0/y0/x1/y1


def _grid_dim_ok(dim: int, bit: int) -> bool:
    """网格维度剪枝：正、BIT 整数倍、COLS/ROWS ≤ 帧头 1 字节上限（§2）。"""
    return dim > 0 and dim % bit == 0 and dim // bit <= _MAX_GRID_DIM


def _aligned_row_pairs(cands: list[tuple[int, int, int, int]], side: int
                       ) -> dict[tuple[int, int], set[int]]:
    """同 side 候选的左右配对（上边 TL–TR 与下边 BL–BR 共用）：两块 top
    相差 ≤ _SIDE_EQUAL_TOL，右块在左块右侧且留出正网格宽（right >
    left + side，严格不等式天然去重：每对只由左侧块枚举一次）。

    返回 {(left, right): 左块 top 集合}——同 left 可竖向堆叠多个候选，
    各自与右侧配对后共用一个键。按 top 分桶 + 邻域扫描，均摊近似 O(n)
    （issue #37：整屏帧候选可达数千，不可全组合）。
    """
    by_top: dict[int, list[int]] = {}
    for left, top, _, _ in cands:
        by_top.setdefault(top, []).append(left)
    pairs: dict[tuple[int, int], set[int]] = {}
    for top, lefts in by_top.items():
        for left in lefts:
            for t2 in range(top - _SIDE_EQUAL_TOL, top + _SIDE_EQUAL_TOL + 1):
                for right in by_top.get(t2, ()):
                    if right > left + side:
                        pairs.setdefault((left, right), set()).add(top)
    return pairs


def _outermost_quadruple(cands: list[tuple[int, int, int, int]], *, scaled=False) -> _Quad | None:
    """满足矩形约束的角标四元组搜索，取覆盖范围最大者（外沿者即真解）。

    真角标内角四点构成轴对齐严格矩形（ADR-0001）：上下边各自同 top
    （±容差）、左右各自同 left（±容差）、四边同 side；数据网格由内角
    给出（内角 = 网格外角），再施加栅格整除性 / COLS·ROWS 1 字节上限
    剪枝。画布内的假矩形（数据白块四人成组）必然严格落在真矩形内部，
    外沿者即真解（issue #37）；画布外围干扰四元组若被误选，由帧头交叉
    校验 + CRC 兜底整帧拒绝，不产生静默错数据。

    返回 (side, 网格矩形 x0/y0/x1/y1)，无满足约束的四元组返回 None。
    """
    by_side: dict[int, list[tuple[int, int, int, int]]] = {}
    for box in cands:
        by_side.setdefault(box[2], []).append(box)
    best: _Quad | None = None
    tols = range(-_SIDE_EQUAL_TOL, _SIDE_EQUAL_TOL + 1)
    for side, boxes in by_side.items():
        bit = side // 3
        if bit <= 0:
            continue
        row_pairs = _aligned_row_pairs(boxes, side)
        for (left, right), top_tops in row_pairs.items():
            gw = right - left - side
            if not (0 < gw <= (side + 1) * 255 / 3 if scaled else _grid_dim_ok(gw, bit)):
                continue
            # 下边对与上边对按 left/right 对齐（±容差）配对成四元组
            for dtl in tols:
                for dtr in tols:
                    for bot_top in row_pairs.get((left + dtl, right + dtr), ()):
                        for top_top in top_tops:
                            gh = bot_top - top_top - side
                            if not (0 < gh <= (side + 1) * 255 / 3 if scaled else _grid_dim_ok(gh, bit)):
                                continue
                            if best is None or gw * gh > best.area:
                                best = _Quad(gw * gh, side, (left + side, top_top + side, right, bot_top))
    return best


def measure_geometry(bw: np.ndarray) -> tuple[MeasuredGeometry, tuple[int, int, int, int]]:
    """几何自举测量（ADR-0001）：检测角标 → BIT → 数据网格矩形 → COLS/ROWS/PAD。

    角标选取为全局四元组矩形约束搜索（issue #37，取代逐角就近——
    「角标必然比任何网格白块更靠近画面角」在整屏采集下不成立）。选取
    阶段已施加栅格整除性剪枝，精确源（PNG / desktop）返回值恒满足
    bootstrap_geometry 的测量层校验；降质源（camera）±1px 偏差整帧拒绝
    的行为不变。calibrate 探针（#11）的边长残差统计口径随之由四角均值
    改为四元组单一边长（精确源下数值等价）。测量不可能（无候选 / 无
    有效四元组）抛 FrameRejected('geometry')。
    """
    cands = _solid_square_candidates(bw)
    if not cands:
        raise FrameRejected("geometry", "未检测到角标候选")
    found = _outermost_quadruple(cands)
    if found is None:
        raise FrameRejected(
            "geometry",
            "未检测到满足矩形约束的角标四元组（角标被遮挡或受干扰；"
            "整屏采集时桌面白色元素易抢占角标，建议 --region 框选画布）")

    side, (x0, y0, x1, y1) = found.side, found.rect
    bit = side // 3
    gw, gh = x1 - x0, y1 - y0
    cols, rows = gw // bit, gh // bit

    # PAD 原始测量：网格原点 ÷ BIT（「画面即画布」时等于真实 PAD，裁切
    # 输入下混入偏移仅供探针参考）；真实 PAD 由帧头交叉校验裁定（#22）
    return MeasuredGeometry(bit=bit, pad=x0 // bit, cols=cols, rows=rows, side=side), \
        (x0, y0, x1, y1)


def bootstrap_geometry(bw: np.ndarray) -> tuple[MeasuredGeometry, tuple[int, int, int, int]]:
    """几何自举测量 + 测量层冻结校验（§5）。

    返回 (测量几何, 网格矩形 x0/y0/x1/y1)。角标边长 / 网格尺寸的整除性
    在此校验；原点对齐与 PAD 一致性校验随裁切容忍退役（issue #22）——
    PAD 由帧头声明，几何上不可测。任一测量约束不满足抛
    FrameRejected('geometry')。
    """
    geo, rect = measure_geometry(bw)
    x0, y0, x1, y1 = rect
    gw, gh = x1 - x0, y1 - y0
    bit = geo.bit
    if geo.side % 3 != 0:
        # 防御性护栏：四元组选取（#37）已保证 side 为候选实测值、候选过滤
        # 已保证 3 整除，此处当前不可达；保留作测量实现的回归护栏
        raise FrameRejected("geometry", f"角标边长 {geo.side} 不能被 3 整除")
    if gw % bit != 0 or gh % bit != 0:
        raise FrameRejected("geometry", f"网格尺寸 {gw}×{gh} 不是 BIT={bit} 的整数倍")
    return geo, rect


@lru_cache(maxsize=16)
def _sample_indices(rows, cols, bit, rect, shape):
    x0, y0, _, _ = rect
    k = min(5, bit)
    if k % 2 == 0:
        k -= 1
    half = k // 2
    centers_y = (y0 + (np.arange(rows) + 0.5) * bit).astype(int)
    centers_x = (x0 + (np.arange(cols) + 0.5) * bit).astype(int)
    return (k, np.clip(centers_y - half, 0, shape[0] - k),
            np.clip(centers_x - half, 0, shape[1] - k))


def _sample_grid(bw: np.ndarray, geo: MeasuredGeometry, rect: tuple[int, int, int, int]) -> bytes:
    """按 BIT 网格采样：格心 k×k 邻域（k = min(5, BIT) 取奇）均值 < 128 = 暗多数 = 1。

    黑块 = 1（§1），行优先、字节内 MSB first。bw 经 Otsu 二值化后只含
    0/255，邻域均值即暗多数表决，不存在 ±1px 级的取整边界歧义。
    局部采样（issue #30 C2）：sliding_window_view 取格心窗口视图后按格心
    gather（O(格心×k²)），不再对整图做 boxFilter（O(像素)）；窗口起点钳制
    到图像边界，与原 BORDER_REPLICATE 等价。
    """
    if geo.pitch_x is not None:
        x0, y0, _, _ = rect
        xs = x0 + (np.arange(geo.cols) + 0.5) * geo.pitch_x
        ys = y0 + (np.arange(geo.rows) + 0.5) * geo.pitch_y
        return _sample_centers(bw, xs[None, :], ys[:, None],
                               min(geo.pitch_x, geo.pitch_y))
    k, rows_sel, cols_sel = _sample_indices(geo.rows, geo.cols, geo.bit, rect, bw.shape)
    win = np.lib.stride_tricks.sliding_window_view(bw, (k, k))  # (H-k+1, W-k+1, k, k) 视图
    window = win[rows_sel[:, None], cols_sel[None, :]]  # → (rows, cols, k, k)
    mean = window.mean(axis=(2, 3))  # 白色占比（0–255）
    bits = (mean < 128).astype(np.uint8)  # 暗多数 = 黑块 = 1
    return np.packbits(bits.flatten(), bitorder="big").tobytes()


def _sample_centers(bw, xs, ys, pitch):
    """非整数格距按格心采样，邻域限制在方块内部，避免缩放边缘混色。"""
    xs = np.floor(xs).astype(int)
    ys = np.floor(ys).astype(int)
    radius = 1 if pitch >= 5 else 0
    values = sum(bw[np.clip(ys + dy, 0, bw.shape[0] - 1),
                    np.clip(xs + dx, 0, bw.shape[1] - 1)].astype(np.uint16)
                 for dy in range(-radius, radius + 1)
                 for dx in range(-radius, radius + 1))
    bits = values < 128 * (2 * radius + 1) ** 2
    return np.packbits(bits.flatten(), bitorder="big").tobytes()


def _decode_scaled(bw, *, marker_mask=None):
    """从缩放角标估计有限 COLS 候选，帧头交叉验证后再用完整 CRC 仲裁。"""
    n, _, stats, _ = cv2.connectedComponentsWithStats(
        bw if marker_mask is None else marker_mask, connectivity=4)
    candidates = []
    for left, top, w, h, area in stats[1:n]:
        if min(w, h) >= 3 and abs(w - h) <= 1 and area >= 0.9 * w * h:
            side = int(round((w + h) / 2))
            candidates.append((int(left), int(top), side, side))
    quad = _outermost_quadruple(candidates, scaled=True)
    if quad is None:
        raise FrameRejected("geometry", "未检测到完整的缩放角标矩形")
    x0, y0, x1, y1 = quad.rect
    gw, gh = x1 - x0, y1 - y0
    # 角标宽度最多有一像素量化误差，搜索范围由该误差界定。
    low = max(1, int(gw * 3 / (quad.side + 1)) - 1)
    high = min(255, int(np.ceil(gw * 3 / max(1, quad.side - 1))) + 1)
    indices = np.arange(HEADER_BYTES * 8)
    for cols in range(low, high + 1):
        pitch_x = gw / cols
        header_bytes = _sample_centers(
            bw, x0 + (indices % cols + 0.5) * pitch_x,
            y0 + (indices // cols + 0.5) * pitch_x, pitch_x)
        try:
            h = parse_header(header_bytes)
        except FrameRejected:
            continue
        if h.cols != cols or h.rows == 0 or h.bit == 0 or h.pad < 3:
            continue
        pitch_y = gh / h.rows
        if (abs(3 * pitch_x - quad.side) > 1.5
                or abs(3 * pitch_y - quad.side) > 1.5
                or abs(pitch_x - pitch_y) > 0.15 * pitch_x):
            continue
        geo = MeasuredGeometry(h.bit, h.pad, cols, h.rows, quad.side,
                               pitch_x, pitch_y)
        return _decode_grid(bw, geo, quad.rect), geo, quad.rect
    raise FrameRejected("geometry", "缩放网格候选未通过帧头验证")


def _decode_display(bw):
    """隔离黑色视频画布，避免外围白边粘连角标；数据始终从原图采样。"""
    try:
        return _decode_scaled(bw)
    except FrameRejected as initial:
        if initial.reason == "crc":
            raise
        best_error = initial
    # 视频黑边通常是最大的黑色连通域，其外接框包含四角及全部网格。
    # 最多检查三个候选；候选裁切只能帮助定位，帧头及 CRC 不可省略。
    n, _, stats, _ = cv2.connectedComponentsWithStats(255 - bw, connectivity=4)
    boxes = sorted(stats[1:n], key=lambda box: int(box[4]), reverse=True)[:3]
    kernel = np.ones((3, 3), dtype=np.uint8)
    for left, top, width, height, _ in boxes:
        if width < 32 or height < 32:
            continue
        canvas = bw[top:top + height, left:left + width]
        # 开运算只用于角标定位，去掉压缩/缩放产生的单像素连接线。
        # 原始数据块即使有细节变化，也仍须在未处理的 canvas 上通过 CRC。
        markers = cv2.morphologyEx(canvas, cv2.MORPH_OPEN, kernel)
        try:
            frame, geo, rect = _decode_scaled(canvas, marker_mask=markers)
        except FrameRejected as error:
            if error.reason == "crc":
                best_error = error
            continue
        x0, y0, x1, y1 = rect
        return frame, geo, (int(x0 + left), int(y0 + top),
                            int(x1 + left), int(y1 + top))
    raise best_error


@dataclass
class GeometryCache:
    """角标检测缓存（issue #30 C1）：帧间几何不变时跳过全量连通域检测。

    connectedComponentsWithStats 是 O(像素)，但播放期间几何不变。首帧完整
    校验后缓存 (geo, rect)，后续帧优先用固定阈值和缓存坐标解码，完整帧头
    与 CRC 失败才回退全量路径。回退时检查缓存网格矩形外推出的四个
    角标框（角标内角 = 网格外角，见 measure_geometry）仍为实心白方块
    （O(side²)）。校验失败（画面尺寸变化 / 裁切偏移漂移）回退
    全量检测并刷新缓存；误命中由帧头交叉校验 + CRC 兜底（整帧拒绝）。
    """

    allow_scaled: bool = False
    _shape: tuple[int, int] | None = None
    _geo: MeasuredGeometry | None = None
    _rect: tuple[int, int, int, int] | None = None
    _validated: bool = False

    def measure(self, bw: np.ndarray) -> tuple[MeasuredGeometry, tuple[int, int, int, int]]:
        """带缓存的几何测量：命中轻量校验直接复用，否则全量检测（含冻结校验）。"""
        if self._geo is None or bw.shape != self._shape or not self._corners_intact(bw):
            self._validated = False
            self._shape = bw.shape
            self._geo, self._rect = bootstrap_geometry(bw)
        return self._geo, self._rect

    def _corners_intact(self, bw: np.ndarray) -> bool:
        x0, y0, x1, y1 = self._rect
        side = self._geo.side
        img_h, img_w = bw.shape
        for left, top in ((x0 - side, y0 - side), (x1, y0 - side),
                          (x0 - side, y1), (x1, y1)):
            if left < 0 or top < 0 or left + side > img_w or top + side > img_h:
                return False
            if not bw[top:top + side, left:left + side].all():
                return False
        return True


def decode_frame(img: np.ndarray, geo_cache: GeometryCache | None = None) -> DecodedFrame:
    """一帧画面 → 解码结果。任一冻结校验不过抛 FrameRejected（整帧丢弃）。

    geo_cache 非 None 时几何测量走缓存（C1，issue #30）：同一接收会话内
    帧间几何不变，跳过全量角标检测；调用方每会话新建一个即可。

    PAD 裁切容忍（issue #22）：网格矩形来自角标、与画布外沿无关，画面
    可在任意偏移处被裁切采集（区域框选「框大一点」）。COLS / ROWS / BIT
    照旧测量并交叉校验；PAD 几何上不可测（画布黑边不可见），以帧头
    声明为准，CRC 保证帧头完整性。
    """
    if img.ndim != 2:
        raise FrameRejected("geometry", "画面不是单通道灰度图")
    if (geo_cache is not None and geo_cache._validated
            and img.shape == geo_cache._shape):
        # 几何来自此前成功识别的画面；清晰桌面先用固定阈值快速采样。
        # 角标局部遮挡也可尝试缓存坐标，但必须完整通过帧头及 CRC。
        # 位移、灰度退化或数据遮挡导致失败时，再走 Otsu + 几何自举。
        _, quick = cv2.threshold(img, 127, 255, cv2.THRESH_BINARY)
        try:
            return _decode_grid(quick, geo_cache._geo, geo_cache._rect)
        except FrameRejected:
            pass
    _, bw = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)

    try:
        if geo_cache is not None:
            geo, rect = geo_cache.measure(bw)
        else:
            geo, rect = bootstrap_geometry(bw)
        frame = _decode_grid(bw, geo, rect)
    except FrameRejected as original:
        if (geo_cache is None or not geo_cache.allow_scaled
                or original.reason not in ("geometry", "sync", "ver")):
            raise
        try:
            frame, geo, rect = _decode_display(bw)
        except FrameRejected as scaled:
            if scaled.reason == "geometry":
                raise original
            raise
        geo_cache._shape = bw.shape
        geo_cache._geo, geo_cache._rect = geo, rect
    if geo_cache is not None:
        geo_cache._validated = True
    return frame


def _decode_grid(bw, geo, rect):
    grid_bytes = _sample_grid(bw, geo, rect)
    if len(grid_bytes) < HEADER_BYTES:
        raise FrameRejected("geometry", f"网格容量 {len(grid_bytes)} 字节装不下帧头")
    header = grid_bytes[:HEADER_BYTES]
    h = parse_header(header)

    # 与帧头交叉校验（§5）：测得几何 vs 帧头 GEO / COLS / ROWS。
    # PAD 不参与几何交叉校验（裁切下不可测，#22）；其完整性由 CRC 覆盖。
    mismatch = []
    if geo.cols != h.cols:
        mismatch.append(f"COLS 测得 {geo.cols} vs 帧头 {h.cols}")
    if geo.rows != h.rows:
        mismatch.append(f"ROWS 测得 {geo.rows} vs 帧头 {h.rows}")
    if geo.bit != h.bit:
        mismatch.append(f"BIT 测得 {geo.bit} vs 帧头 {h.bit}")
    if mismatch:
        raise FrameRejected("geometry", "；".join(mismatch))
    if (h.cols * h.rows) % 8 != 0:
        # §1 冻结约束：数据区字节对齐；不满足即坏帧，而非静默错位解码
        raise FrameRejected("geometry", f"(COLS×ROWS) mod 8 != 0（{h.cols}×{h.rows}）")

    capacity = len(grid_bytes) - HEADER_BYTES
    if h.data_len > capacity:
        raise FrameRejected("data_len", f"DATA_LEN={h.data_len} 超出网格容量 {capacity}")
    is_fec = bool(h.flags & FLAGS_FEC)
    if h.flags & FLAGS_REPAIR:
        if not h.total_frames or not h.total_frames <= h.frame_no < FRAME_NO_METADATA:
            raise FrameRejected("frame_no", "喷泉码修复帧号越界")
        if h.data_len != h.chunk_size:
            raise FrameRejected("data_len", "修复帧必须携带完整 CHUNK_SIZE")
    elif is_fec:
        if not is_fec_frame(h.frame_no, h.total_frames):
            raise FrameRejected(
                "frame_no",
                f"FEC 帧号 {h.frame_no} 不在 [{h.total_frames}, "
                f"{h.total_frames + fec_parity_count(h.total_frames)})",
            )
        if h.data_len != h.chunk_size:
            raise FrameRejected("data_len", "FEC 校验帧必须携带完整 CHUNK_SIZE")
    elif h.frame_no != FRAME_NO_METADATA and h.frame_no >= h.total_frames:
        raise FrameRejected("frame_no", f"{h.frame_no} ≥ TOTAL_FRAMES {h.total_frames}")
    if h.flags & ~FLAGS_ALLOWED:
        raise FrameRejected("flags", f"保留位非 0: 0x{h.flags:04X}")

    verify_crc(header, grid_bytes[HEADER_BYTES:])
    payload = grid_bytes[HEADER_BYTES : HEADER_BYTES + h.data_len]
    if h.frame_no == FRAME_NO_METADATA:
        # §4：12 + nameLen 超出本帧 CHUNK_SIZE 的元数据帧整帧丢弃并告警，
        # 不影响数据帧接收；nameLen 以数据区前 11 字节声明值为准
        if len(payload) < 11:
            raise FrameRejected("metadata", f"元数据帧数据区 {len(payload)} 字节不足")
        name_len = int.from_bytes(payload[9:11], "big")
        if METADATA_FIXED_BYTES + name_len > h.chunk_size:
            raise FrameRejected(
                "metadata",
                f"12+nameLen={METADATA_FIXED_BYTES + name_len} 超出 CHUNK_SIZE {h.chunk_size}",
            )
        return DecodedFrame(header=h, payload=payload, metadata=parse_metadata(payload))
    return DecodedFrame(header=h, payload=payload)
