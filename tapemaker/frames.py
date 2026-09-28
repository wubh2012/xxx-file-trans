"""帧序列构建（制带的协议写入侧，issue #38）。

sender.html 之外的第二份封帧实现：CRC / FEC 语义全部落在
`receiver.protocol` / `receiver.fec`（单一事实来源，防协议漂移），
本模块只负责写侧组装——帧头字节填充、分片、元数据帧（§4）、
FEC 校验帧（§5）与轮次重复（ADR-0003）。

一轮序列布局与 sender.html 播放序列逐位对齐：
  - 元数据帧按「每轮首帧 + 每 100 数据帧」节奏插入（帧号 0xFFFFFF 哨兵）；
  - FEC 校验帧紧跟所在组（每 32 数据帧 + 2 校验帧），帧号从 TOTAL_FRAMES
    起连续编号；
  - 整轮序列连播 N 轮（N 由调用方重复），接收端按帧号幂等落盘。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from receiver.fec import gf_mul
from receiver.protocol import (
    FEC_GROUP_SIZE,
    FEC_PARITY_FRAMES,
    FLAGS_FEC,
    FLAGS_GZIP,
    FRAME_NO_METADATA,
    HEADER_BITS,
    fec_parity_count,
    frame_crc,
)

METADATA_FIXED_BYTES = 12  # 元数据帧数据区固定部分（§4：1+4+4+2+1）
METADATA_INTERVAL = 100  # 每 100 数据帧插一帧元数据（§4）

GZIP_FLAGS = FLAGS_GZIP
PARITY_FLAGS = FLAGS_GZIP | FLAGS_FEC  # 与 sender.html 的 FEC_FLAGS（0xC000）一致

# 分辨率档 → 画布物理像素（视频帧尺寸）。默认 BIT 随档位给出：COLS/ROWS
# 受帧头 1 字节上限（≤255）约束——1080p 下 BIT < 8 会推出 COLS > 255，
# 4K 下 BIT 下限更高（见 derive_geometry 的校验）。
RESOLUTIONS = {"1080p": (1920, 1080), "4k": (3840, 2160)}
DEFAULT_BIT = {"1080p": 8, "4k": 15}


@dataclass(frozen=True)
class Geometry:
    """帧几何（§6 冻结约定）：画布 = (COLS+2×PAD)×BIT × (ROWS+2×PAD)×BIT。"""

    cols: int
    rows: int
    bit: int
    pad: int
    canvas_w: int
    canvas_h: int

    @property
    def chunk_size(self) -> int:
        """单帧数据区整字节容量：208 bit 帧头之外的全部字节。"""
        return (self.cols * self.rows - HEADER_BITS) // 8


def derive_geometry(canvas_w: int, canvas_h: int, bit: int, pad: int) -> Geometry:
    """按画布与 BIT / PAD 反推 COLS / ROWS（与 sender.html deriveGeometry 同一口径：
    (COLS×ROWS) mod 8 == 0 冻结约束，不足时递减 ROWS）。"""
    if not 1 <= bit <= 15:
        raise ValueError(f"BIT={bit} 超出帧头 4 bit 上限（1–15）")
    if not 3 <= pad <= 15:
        raise ValueError(f"PAD={pad} 超出支持范围（3–15）：PAD < 3 放不下 3×BIT 角标")
    cols = canvas_w // bit - 2 * pad
    rows = canvas_h // bit - 2 * pad
    while rows > 0 and (cols * rows) % 8 != 0:
        rows -= 1
    if cols <= 0 or rows <= 0:
        raise ValueError(f"BIT={bit} / PAD={pad} 在 {canvas_w}×{canvas_h} 下放不下数据网格")
    if cols > 255 or rows > 255:
        raise ValueError(
            f"COLS={cols} / ROWS={rows} 超出帧头 1 字节上限（≤255）：请增大 --bit"
        )
    return Geometry(cols, rows, bit, pad, canvas_w, canvas_h)


@dataclass(frozen=True)
class Frame:
    """一帧的完整字节：26 字节帧头 + 数据区（定长 CHUNK_SIZE，末帧零补齐）。"""

    frame_no: int  # 数据帧号 / 0xFFFFFF 元数据哨兵 / TOTAL_FRAMES 起的校验帧号
    flags: int
    header: bytes
    data: bytes  # 恒为 CHUNK_SIZE 长（CRC 与渲染均按全长处理）


def build_header(
    file_id: int,
    frame_no: int,
    total_frames: int,
    data_len: int,
    geo: Geometry,
    flags: int,
) -> bytearray:
    """26 字节帧头（§3，大端序）：CRC 待填（偏移 22–25）。"""
    h = bytearray(26)
    h[0:2] = b"\xF0\xA5"
    h[2] = 0x01  # VER
    h[3:7] = file_id.to_bytes(4, "big")
    h[7:10] = frame_no.to_bytes(3, "big")
    h[10:13] = total_frames.to_bytes(3, "big")
    h[13:15] = data_len.to_bytes(2, "big")
    h[15:17] = geo.chunk_size.to_bytes(2, "big")
    h[17] = geo.cols
    h[18] = geo.rows
    h[19] = (geo.bit << 4) | geo.pad
    h[20:22] = flags.to_bytes(2, "big")
    return h


def metadata_payload(filename: str, plain_size: int, compressed_size: int) -> bytes:
    """元数据帧数据区：12 + nameLen 布局（§4，大端序）。"""
    name = filename.encode("utf-8")
    return (
        b"\x01"  # 压缩方式：gzip
        + plain_size.to_bytes(4, "big")
        + compressed_size.to_bytes(4, "big")
        + len(name).to_bytes(2, "big")
        + b"\x00"  # 保留
        + name
    )


class _ParityEncoder:
    """FEC 校验分片计算（§5）：P0 = 组内逐字节 XOR；P1 = 组内第 i 个分片乘
    GF(256) 系数 i+1 后逐字节 XOR。乘法表由 receiver.fec.gf_mul 生成——
    与接收端恢复用同一 GF 实现，系数语义不可能漂移。"""

    def __init__(self) -> None:
        table = np.empty((FEC_GROUP_SIZE, 256), dtype=np.uint8)
        for coeff in range(1, FEC_GROUP_SIZE + 1):
            table[coeff - 1] = [gf_mul(coeff, v) for v in range(256)]
        self._table = table
        self._rows = np.arange(FEC_GROUP_SIZE)[:, None]

    def parity(self, group: np.ndarray) -> tuple[bytes, bytes]:
        """group: (n, CHUNK_SIZE) uint8（组内数据分片，不足一组按实际帧数）。"""
        p0 = np.bitwise_xor.reduce(group, axis=0)
        p1 = np.bitwise_xor.reduce(self._table[self._rows[: len(group)], group], axis=0)
        return p0.tobytes(), p1.tobytes()


def build_round(
    payload: bytes,
    file_id: int,
    filename: str,
    plain_size: int,
    geo: Geometry,
) -> list[Frame]:
    """一轮完整帧序列：元数据帧 + 数据帧 + FEC 校验帧（发送端播放序列口径）。

    payload 为 gzip 压缩后的全部字节。布局约束（协议上限）在此拦截：
    12 + nameLen 超出 CHUNK_SIZE 的元数据帧接收端必整帧丢弃（§4），
    文件将无法还原且无提示，发送侧必须直接拒绝。
    """
    chunk = geo.chunk_size
    total = (len(payload) + chunk - 1) // chunk
    name_bytes = filename.encode("utf-8")
    if METADATA_FIXED_BYTES + len(name_bytes) > chunk:
        raise ValueError(
            f"文件名过长：{METADATA_FIXED_BYTES}+nameLen={METADATA_FIXED_BYTES + len(name_bytes)}"
            f" 超出单帧 CHUNK_SIZE {chunk}，元数据帧无法承载"
        )
    if total + fec_parity_count(total) > 0xFFFFFF:
        raise ValueError(f"文件过大：数据帧 + FEC 帧号超出 3 字节上限（0xFFFFFF）")

    meta_data = metadata_payload(filename, plain_size, len(payload))
    parity_enc = _ParityEncoder()

    frames: list[Frame] = []
    group_chunks: list[np.ndarray] = []

    def emit(header: bytearray, data: bytes, frame_no: int, flags: int) -> None:
        header[22:26] = frame_crc(bytes(header), data).to_bytes(4, "big")
        frames.append(Frame(frame_no, flags, bytes(header), data))

    def flush_group(last_frame_no: int) -> None:
        """当前组凑满（或末组收尾）→ 两个校验帧，帧号从 TOTAL_FRAMES 起连续。"""
        if not group_chunks:
            return
        group_no = last_frame_no // FEC_GROUP_SIZE
        p0, p1 = parity_enc.parity(np.array(group_chunks))
        base = total + group_no * FEC_PARITY_FRAMES
        for k, p in enumerate((p0, p1)):
            h = build_header(file_id, base + k, total, chunk, geo, PARITY_FLAGS)
            emit(h, p, base + k, PARITY_FLAGS)
        group_chunks.clear()

    for frame_no in range(total):
        if frame_no % METADATA_INTERVAL == 0:
            h = build_header(
                file_id, FRAME_NO_METADATA, total, len(meta_data), geo, GZIP_FLAGS
            )
            emit(h, meta_data.ljust(chunk, b"\x00"), FRAME_NO_METADATA, GZIP_FLAGS)
        data = payload[frame_no * chunk : (frame_no + 1) * chunk]
        padded = data.ljust(chunk, b"\x00")  # 末帧零补齐（§5：FEC 前补齐）
        h = build_header(file_id, frame_no, total, len(data), geo, GZIP_FLAGS)
        emit(h, padded, frame_no, GZIP_FLAGS)
        group_chunks.append(np.frombuffer(padded, dtype=np.uint8))
        if frame_no % FEC_GROUP_SIZE == FEC_GROUP_SIZE - 1 or frame_no == total - 1:
            flush_group(frame_no)
    return frames
