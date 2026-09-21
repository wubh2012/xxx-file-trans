"""还原：gunzip + 尾部 CRC / ISIZE 端到端验证（docs/protocol.md / F13）。

gzip（RFC 1952）尾部 8 字节 = 原始内容 CRC32（小端）+ ISIZE（原始长度
mod 2^32，小端）。zlib.decompressobj(wbits=31) 在流结束时会校验二者，
此处再显式比对尾部字段，双重证明后落盘。
"""

import struct
import zlib


class RestoreError(Exception):
    pass


def gunzip_verify(comp: bytes) -> bytes:
    """解压 gzip 流并验证尾部 CRC32 + ISIZE。任一不过抛 RestoreError。"""
    if len(comp) < 18:  # 10B 头 + 8B 尾
        raise RestoreError(f"gzip 流过短（{len(comp)} 字节），缺少头部或尾部")
    d = zlib.decompressobj(31)
    try:
        plain = d.decompress(comp) + d.flush()
    except zlib.error as e:
        raise RestoreError(f"gzip 解压失败（尾部 CRC/ISIZE 校验不通过）: {e}") from e
    if not d.eof or d.unused_data:
        raise RestoreError("gzip 流不完整或含尾随数据")
    crc_trailer, isize_trailer = struct.unpack("<II", comp[-8:])
    if zlib.crc32(plain) & 0xFFFFFFFF != crc_trailer:
        raise RestoreError(f"尾部 CRC 不一致：期望 0x{crc_trailer:08X}，实际 0x{zlib.crc32(plain) & 0xFFFFFFFF:08X}")
    if len(plain) % 2**32 != isize_trailer:
        raise RestoreError(f"尾部 ISIZE 不一致：期望 {isize_trailer}，实际 {len(plain) % 2**32}")
    return plain
