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

import cv2
import numpy as np

from receiver.metadata import FIXED_BYTES as METADATA_FIXED_BYTES
from receiver.metadata import FileMetadata, parse_metadata
from receiver.protocol import (
    FRAME_NO_METADATA,
    HEADER_BYTES,
    SYNC,
    VER,
    FrameRejected,
    verify_crc,
)

# 角标四条几何一致性约束的容差：PNG 源画面精确，透视/采集源由后续
# 票据（desktop #9 / camera #13）在此处扩展校正逻辑。
_SIDE_EQUAL_TOL = 1  # 四角标边长允许的测量偏差（像素）


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


def parse_header(header: bytes) -> FrameHeader:
    """解析 26 字节帧头（§2）。SYNC / VER 不符整帧丢弃。"""
    sync = int.from_bytes(header[0:2], "big")
    if sync != SYNC:
        raise FrameRejected("sync", f"期望 0x{SYNC:04X}，实际 0x{sync:04X}")
    if header[2] != VER:
        raise FrameRejected("ver", f"期望 0x{VER:02X}，实际 0x{header[2]:02X}")
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


def _corner_distance(box: tuple[int, int, int, int], corner: str, img_w: int, img_h: int) -> int:
    """外接框到画面角的切比雪夫距离（角标必然比任何网格白块更靠近画面角）。"""
    left, top, w, h = box
    right, bottom = left + w, top + h
    dist = {
        "tl": lambda: max(left, top),
        "tr": lambda: max(img_w - right, top),
        "bl": lambda: max(left, img_h - bottom),
        "br": lambda: max(img_w - right, img_h - bottom),
    }
    return dist[corner]()


def measure_geometry(bw: np.ndarray) -> tuple[MeasuredGeometry, tuple[int, int, int, int]]:
    """几何自举测量（ADR-0001）：检测角标 → BIT → 数据网格矩形 → COLS/ROWS/PAD。

    只测量、不做栅格整除性 / PAD 一致性等冻结校验（后者在 bootstrap_geometry）；
    calibrate 探针（#11）需要未校验的原始测量值统计 ±1px 级残差。
    测量本身不可能（无角标候选等）仍抛 FrameRejected('geometry')。
    """
    img_h, img_w = bw.shape
    cands = _solid_square_candidates(bw)

    # 四角各取距画面角最近的候选，再施加四条几何一致性约束
    boxes: dict[str, tuple[int, int, int, int]] = {}
    for corner in ("tl", "tr", "bl", "br"):
        if not cands:
            raise FrameRejected("geometry", "未检测到角标候选")
        boxes[corner] = min(cands, key=lambda b: _corner_distance(b, corner, img_w, img_h))

    sides = [boxes[c][2] for c in ("tl", "tr", "bl", "br")]
    if max(sides) - min(sides) > _SIDE_EQUAL_TOL:
        raise FrameRejected("geometry", f"四角标边长不一致: {sides}")
    side = sum(sides) // 4
    bit = side // 3

    # 角标内角 = 数据网格四角外角 → 网格矩形
    x0 = boxes["tl"][0] + side
    y0 = boxes["tl"][1] + side
    x1 = boxes["tr"][0]
    y1 = boxes["bl"][1]
    gw, gh = x1 - x0, y1 - y0
    if gw <= 0 or gh <= 0:
        raise FrameRejected("geometry", f"数据网格尺寸简并: {gw}×{gh}")
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
        raise FrameRejected("geometry", f"角标边长 {geo.side} 不能被 3 整除")
    if gw % bit != 0 or gh % bit != 0:
        raise FrameRejected("geometry", f"网格尺寸 {gw}×{gh} 不是 BIT={bit} 的整数倍")
    return geo, rect


def _sample_grid(bw: np.ndarray, geo: MeasuredGeometry, rect: tuple[int, int, int, int]) -> bytes:
    """按 BIT 网格采样：格心 k×k 邻域（k = min(5, BIT) 取奇）均值 < 128 = 暗多数 = 1。

    黑块 = 1（§1），行优先、字节内 MSB first。bw 经 Otsu 二值化后只含
    0/255，邻域均值即暗多数表决，不存在 ±1px 级的取整边界歧义。
    局部采样（issue #30 C2）：按格心收集邻域（O(格心×k²)），不再对整图
    做 boxFilter（O(像素)）；索引越界处复制边缘，与原 BORDER_REPLICATE 等价。
    """
    x0, y0, _, _ = rect
    k = min(5, geo.bit)
    if k % 2 == 0:
        k -= 1
    half = k // 2
    img_h, img_w = bw.shape
    centers_y = (y0 + (np.arange(geo.rows) + 0.5) * geo.bit).astype(int)
    centers_x = (x0 + (np.arange(geo.cols) + 0.5) * geo.bit).astype(int)
    offs = np.arange(-half, half + 1)
    ys = np.clip(centers_y[:, None, None, None] + offs[None, None, :, None], 0, img_h - 1)  # (rows, 1, k, 1)
    xs = np.clip(centers_x[None, :, None, None] + offs[None, None, None, :], 0, img_w - 1)  # (1, cols, 1, k)
    window = bw[ys, xs]  # 广播 → (rows, cols, k, k)
    mean = window.mean(axis=(2, 3))  # 白色占比（0–255）
    bits = (mean < 128).astype(np.uint8)  # 暗多数 = 黑块 = 1
    return np.packbits(bits.flatten(), bitorder="big").tobytes()


@dataclass
class GeometryCache:
    """角标检测缓存（issue #30 C1）：帧间几何不变时跳过全量连通域检测。

    connectedComponentsWithStats 是 O(像素)，但播放期间几何不变。首帧全量
    检测后缓存 (geo, rect)，后续帧只做轻量校验：缓存网格矩形外推出的四个
    角标框（角标内角 = 网格外角，见 measure_geometry）仍为实心白方块
    （O(side²)）。校验失败（角标被破坏 / 画面尺寸变化 / 裁切偏移漂移）回退
    全量检测并刷新缓存；误命中由帧头交叉校验 + CRC 兜底（整帧拒绝）。
    """

    _shape: tuple[int, int] | None = None
    _geo: MeasuredGeometry | None = None
    _rect: tuple[int, int, int, int] | None = None

    def measure(self, bw: np.ndarray) -> tuple[MeasuredGeometry, tuple[int, int, int, int]]:
        """带缓存的几何测量：命中轻量校验直接复用，否则全量检测（含冻结校验）。"""
        if self._geo is None or bw.shape != self._shape or not self._corners_intact(bw):
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
    _, bw = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)

    if geo_cache is not None:
        geo, rect = geo_cache.measure(bw)
    else:
        geo, rect = bootstrap_geometry(bw)
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
    if h.frame_no != FRAME_NO_METADATA and h.frame_no >= h.total_frames:
        raise FrameRejected("frame_no", f"{h.frame_no} ≥ TOTAL_FRAMES {h.total_frames}")
    if h.flags & ~0x8000:
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
