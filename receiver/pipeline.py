"""识别流水线：几何自举（ADR-0001）→ 规范画布采样 → 帧头解析 → 交叉校验。

无默认参数（docs/protocol.md §5 冻结）：BIT 由角标白块边长 ÷ 3 推得，
COLS / ROWS 由四角标定出的数据网格矩形推得，PAD 由画布边沿到网格的
距离推得；再与帧头 GEO / COLS / ROWS 交叉校验，不一致整帧丢弃。

发送端几何约定（sender.html 头注声明为实现基准，接收端据此自举）：
静默区黑色宽 PAD×BIT；角标 = 白色实心方块、边长严格 3×BIT、内角与
数据网格四角外角重合（对角线方向伸入静默区，故 PAD ≥ 3）；黑块 = 1。
PAD 推导假设「画面即画布、静默区贴边」——发送端导出帧满足；采集类源
（desktop #9 / camera #13）引入透视/裁切时在此扩展校正。
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
    """几何自举测量结果（全部来自画面，无默认值）。"""

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

    # PAD：画布边沿到网格距离（画面即画布，发送端导出帧无额外留白）；
    # 整除性 / 四角一致性是冻结校验，交 bootstrap_geometry
    return MeasuredGeometry(bit=bit, pad=x0 // bit, cols=cols, rows=rows, side=side), \
        (x0, y0, x1, y1)


def bootstrap_geometry(bw: np.ndarray) -> tuple[MeasuredGeometry, tuple[int, int, int, int]]:
    """几何自举（ADR-0001）：测量 + 冻结校验（§5）。

    返回 (测量几何, 网格矩形 x0/y0/x1/y1)。任一约束不满足抛 FrameRejected('geometry')。
    """
    geo, rect = measure_geometry(bw)
    x0, y0, x1, y1 = rect
    gw, gh = x1 - x0, y1 - y0
    bit = geo.bit
    if geo.side % 3 != 0:
        raise FrameRejected("geometry", f"角标边长 {geo.side} 不能被 3 整除")
    if gw % bit != 0 or gh % bit != 0:
        raise FrameRejected("geometry", f"网格尺寸 {gw}×{gh} 不是 BIT={bit} 的整数倍")
    if x0 % bit != 0 or y0 % bit != 0 or geo.pad != y0 // bit:
        raise FrameRejected("geometry", f"静默区宽度不一致: x={x0} y={y0}")
    return geo, rect


def _sample_grid(bw: np.ndarray, geo: MeasuredGeometry, rect: tuple[int, int, int, int]) -> bytes:
    """按 BIT 网格采样：单元中心 k×k 窗口（k = min(5, BIT) 取奇）中位数，暗多数 = 1。

    黑块 = 1（§1），行优先、字节内 MSB first。
    """
    x0, y0, _, _ = rect
    k = min(5, geo.bit)
    if k % 2 == 0:
        k -= 1
    mean = cv2.boxFilter(bw, ddepth=-1, ksize=(k, k), borderType=cv2.BORDER_REPLICATE)
    centers_y = (y0 + (np.arange(geo.rows) + 0.5) * geo.bit).astype(int)
    centers_x = (x0 + (np.arange(geo.cols) + 0.5) * geo.bit).astype(int)
    window = mean[np.ix_(centers_y, centers_x)]  # 白色占比（0–255）
    bits = (window < 128).astype(np.uint8)  # 暗多数 = 黑块 = 1
    return np.packbits(bits.flatten(), bitorder="big").tobytes()


def decode_frame(img: np.ndarray) -> DecodedFrame:
    """一帧画面 → 解码结果。任一冻结校验不过抛 FrameRejected（整帧丢弃）。"""
    if img.ndim != 2:
        raise FrameRejected("geometry", "画面不是单通道灰度图")
    _, bw = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)

    geo, rect = bootstrap_geometry(bw)
    grid_bytes = _sample_grid(bw, geo, rect)
    if len(grid_bytes) < HEADER_BYTES:
        raise FrameRejected("geometry", f"网格容量 {len(grid_bytes)} 字节装不下帧头")
    header = grid_bytes[:HEADER_BYTES]
    h = parse_header(header)

    # 与帧头交叉校验（§5）：测得几何 vs 帧头 GEO / COLS / ROWS
    mismatch = []
    if geo.cols != h.cols:
        mismatch.append(f"COLS 测得 {geo.cols} vs 帧头 {h.cols}")
    if geo.rows != h.rows:
        mismatch.append(f"ROWS 测得 {geo.rows} vs 帧头 {h.rows}")
    if geo.bit != h.bit:
        mismatch.append(f"BIT 测得 {geo.bit} vs 帧头 {h.bit}")
    if geo.pad != h.pad:
        mismatch.append(f"PAD 测得 {geo.pad} vs 帧头 {h.pad}")
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
