"""元数据帧数据区解析（docs/protocol.md §4 冻结布局）。

12 + nameLen 布局，大端序：1B 压缩方式 + 4B plainSize + 4B compressedSize
+ 2B nameLen + 1B 保留 + nameLen B 文件名（UTF-8）。
任一冻结校验不过抛 FrameRejected('metadata')——整帧丢弃并告警，
不影响数据帧接收。
"""

from dataclasses import dataclass

from receiver.protocol import FrameRejected

METHOD_GZIP = 0x01
FIXED_BYTES = 12  # 文件名之前的固定布局长度（12 + nameLen，§4）


@dataclass
class FileMetadata:
    """一份文件的元数据（落盘文件名与还原截断长度的唯一来源，§4）。"""

    method: int
    plain_size: int
    compressed_size: int
    name: str


def parse_metadata(payload: bytes) -> FileMetadata:
    """解析元数据帧数据区。布局 / 保留位 / 压缩方式 / UTF-8 不合法即拒绝。"""
    if len(payload) < FIXED_BYTES:
        raise FrameRejected("metadata", f"数据区 {len(payload)} 字节不足 {FIXED_BYTES} 字节固定布局")
    method = payload[0]
    if method != METHOD_GZIP:
        raise FrameRejected("metadata", f"未知压缩方式 0x{method:02X}")
    if payload[11] != 0:
        raise FrameRejected("metadata", f"保留位非 0: 0x{payload[11]:02X}")
    name_len = int.from_bytes(payload[9:11], "big")
    if len(payload) < FIXED_BYTES + name_len:
        raise FrameRejected("metadata", f"nameLen={name_len} 超出数据区 {len(payload)} 字节")
    try:
        name = payload[FIXED_BYTES : FIXED_BYTES + name_len].decode("utf-8")
    except UnicodeDecodeError as e:
        raise FrameRejected("metadata", f"文件名不是合法 UTF-8: {e}") from e
    return FileMetadata(
        method=method,
        plain_size=int.from_bytes(payload[1:5], "big"),
        compressed_size=int.from_bytes(payload[5:9], "big"),
        name=name,
    )
