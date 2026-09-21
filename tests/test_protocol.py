"""协议冻结自检向量（issue #2）。

锁定 docs/protocol.md §3 / §7 的冻结事实：帧 CRC 为 CRC-32/ISO-HDLC，
与 zlib.crc32 同参。用一份纯 Python 位级实现独立推导 CRC，
再与 zlib.crc32 交叉校验，防止任何一端"参数漂移"而不自知。
"""

import zlib

# docs/protocol.md §7 的双端自检向量
SELF_CHECK_INPUT = b"123456789"
SELF_CHECK_EXPECTED = 0xCBF43926


def crc32_iso_hdlc_ref(data: bytes) -> int:
    """纯 Python 位级 CRC-32/ISO-HDLC（反射算法）。

    冻结参数：poly=0x04C11DB7（反射 0xEDB88320）、init=0xFFFFFFFF、
    refin=refout=true、xorout=0xFFFFFFFF —— 与 zlib.crc32 同参。
    独立实现，不 import 任何协议编码代码，兼作交叉校验基准。
    """
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xEDB88320 if crc & 1 else crc >> 1
    return crc ^ 0xFFFFFFFF


def test_self_check_vector_zlib():
    assert zlib.crc32(SELF_CHECK_INPUT) == SELF_CHECK_EXPECTED


def test_self_check_vector_ref_impl():
    assert crc32_iso_hdlc_ref(SELF_CHECK_INPUT) == SELF_CHECK_EXPECTED


def test_ref_impl_matches_zlib_on_samples():
    """多组输入交叉校验，覆盖空串 / 边界字节 / 全 0xFF / 全字节域。"""
    samples = [
        b"",
        b"\x00",
        b"\xff" * 7,
        b"protocol-freeze",
        bytes(range(256)),
    ]
    for s in samples:
        assert zlib.crc32(s) == crc32_iso_hdlc_ref(s), f"CRC mismatch on {s!r}"
