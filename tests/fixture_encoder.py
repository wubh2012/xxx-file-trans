"""测试夹具：独立 Python 最小编码器（spec「测试策略」）。

它是传输协议（docs/protocol.md）的独立实现，与 sender.html / receiver
互不 import，兼作交叉校验基准：夹具产出的 PNG 帧序列经接收端 CLI 缝
（缝 B）解码，验证双端协议一致。仅测试用途，不进入 receiver 包。

几何约定与 sender.html 相同（冻结）：
  - 静默区黑色，宽度 = PAD × BIT 像素；
  - 角标 = 白色实心方块，边长 = 3×BIT，内角与数据网格四角外角重合；
  - 画布总尺寸 = (COLS + 2×PAD) × BIT 乘 (ROWS + 2×PAD) × BIT；
  - 黑块 = 1，行优先、字节内 MSB first。
"""

import zlib
from pathlib import Path

import cv2
import numpy as np

HEADER_BITS = 208
SYNC = b"\xF0\xA5"
VER = 0x01


def frame_capacity(cols: int, rows: int) -> int:
    """一帧数据区整字节容量：208 bit 帧头之外的全部字节。"""
    return (cols * rows - HEADER_BITS) // 8


def crc32(data: bytes) -> int:
    """CRC-32/ISO-HDLC（docs/protocol.md §3，与 zlib.crc32 同参）。"""
    return zlib.crc32(data) & 0xFFFFFFFF


def build_header(
    file_id: int,
    frame_no: int,
    total_frames: int,
    data_len: int,
    chunk_size: int,
    cols: int,
    rows: int,
    bit: int,
    pad: int,
) -> bytes:
    """26 字节帧头，大端序；CRC 覆盖偏移 2–21 + 数据区有效字节。"""
    h = bytearray(26)
    h[0:2] = SYNC
    h[2] = VER
    h[3:7] = file_id.to_bytes(4, "big")
    h[7:10] = frame_no.to_bytes(3, "big")
    h[10:13] = total_frames.to_bytes(3, "big")
    h[13:15] = data_len.to_bytes(2, "big")
    h[15:17] = chunk_size.to_bytes(2, "big")
    h[17] = cols
    h[18] = rows
    h[19] = (bit << 4) | pad
    h[20:22] = (0x8000).to_bytes(2, "big")  # FLAGS：bit15 = gzip
    return bytes(h)


def crc_of(header: bytes, data: bytes) -> int:
    """帧 CRC：覆盖帧头偏移 2–21 + 数据区有效字节（补位不计）。"""
    return crc32(header[2:22] + data[: int.from_bytes(header[13:15], "big")])


def render_png(
    header: bytes,
    payload: bytes,
    bit: int,
    pad: int,
    path,
    cols: int | None = None,
    rows: int | None = None,
) -> None:
    """一帧 → PNG。cols/rows 缺省按帧头字段取值；显式传入可制造几何错配帧。

    错配语义：cols/rows 参数决定**画面实际画多少块**，帧头字段照旧写入
    header——两者不一致即协议要求的「与帧头交叉校验不一致」场景。
    """
    h_cols, h_rows = header[17], header[18]
    cols = h_cols if cols is None else cols
    rows = h_rows if rows is None else rows
    assert (cols * rows) % 8 == 0, "(COLS×ROWS) mod 8 == 0 冻结约束"
    grid_bytes = header + payload  # 帧头 + 数据区，行优先、字节内 MSB first
    assert len(grid_bytes) * 8 <= cols * rows, "数据超出网格容量"

    cw = (cols + 2 * pad) * bit
    ch = (rows + 2 * pad) * bit
    canvas = np.zeros((ch, cw), dtype=np.uint8)  # 静默区黑
    # 数据网格：默认白（bit=0），置位的 bit 涂黑（黑块 = 1）
    grid = np.ones((rows, cols), dtype=np.uint8) * 255
    for byte_idx, b in enumerate(grid_bytes):
        for k in range(7, -1, -1):
            if (b >> k) & 1:
                cell = byte_idx * 8 + (7 - k)
                r, c = divmod(cell, cols)
                if r < rows and c < cols:
                    grid[r, c] = 0
    ox, oy = pad * bit, pad * bit
    canvas[oy : oy + rows * bit, ox : ox + cols * bit] = grid.repeat(bit, axis=0).repeat(bit, axis=1)
    # 角标：白色实心方块，边长 3×BIT，内角贴网格外角（对角线方向伸入静默区）
    m = 3 * bit
    canvas[oy - m : oy, ox - m : ox] = 255
    canvas[oy - m : oy, ox + cols * bit : ox + cols * bit + m] = 255
    canvas[oy + rows * bit : oy + rows * bit + m, ox - m : ox] = 255
    canvas[oy + rows * bit : oy + rows * bit + m, ox + cols * bit : ox + cols * bit + m] = 255

    cv2.imwrite(str(path), canvas)


def signed_header(file_id: int, frame_no: int, total_frames: int, data: bytes,
                  chunk_size: int, cols: int, rows: int, bit: int, pad: int) -> bytes:
    """构建 CRC 已落位的完整帧头（供需要手搓单帧的用例复用）。"""
    header = bytearray(build_header(file_id, frame_no, total_frames, len(data), chunk_size, cols, rows, bit, pad))
    header[22:26] = crc_of(bytes(header), data).to_bytes(4, "big")
    return bytes(header)


def export_frames(
    payload: bytes,
    file_id: int,
    out_dir,
    cols: int = 48,
    rows: int = 48,
    bit: int = 4,
    pad: int = 3,
) -> list:
    """整包 payload 分片封帧并导出 PNG 序列（000001.png 起，发送端 frames_png 布局）。

    返回 PNG 路径列表。几何错配 / 坏 CRC 场景由调用方拿到帧头后自行改写重画。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    chunk = frame_capacity(cols, rows)
    total = (len(payload) + chunk - 1) // chunk
    paths: list[Path] = []
    for frame_no in range(total):
        data = payload[frame_no * chunk : (frame_no + 1) * chunk]
        header = signed_header(file_id, frame_no, total, data, chunk, cols, rows, bit, pad)
        p = out_dir / f"{frame_no + 1:06d}.png"
        render_png(header, data, bit, pad, p)
        paths.append(p)
    return paths
