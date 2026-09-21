"""协议解析（docs/protocol.md 冻结事实的接收端读取侧）。

封帧在发送端；本模块只做帧头字段读取与 CRC 校验，不提供编码。
"""

import zlib

# 帧头布局（§2，26 字节，大端序，MSB first）
HEADER_BITS = 208
HEADER_BYTES = HEADER_BITS // 8
SYNC = 0xF0A5
VER = 0x01
FRAME_NO_METADATA = 0xFFFFFF  # 元数据帧哨兵（§4）


class FrameRejected(Exception):
    """一帧被拒绝。reason 用于日志：sync / ver / crc / geometry / 形状越界等。"""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


SELF_CHECK_INPUT = b"123456789"
SELF_CHECK_EXPECTED = 0xCBF43926


def startup_self_check() -> None:
    """双端启动自检（§3 冻结：两端启动时必须自检通过），不过则拒绝运行。"""
    actual = zlib.crc32(SELF_CHECK_INPUT) & 0xFFFFFFFF
    if actual != SELF_CHECK_EXPECTED:
        raise RuntimeError(
            f"CRC 自检未通过：CRC32({SELF_CHECK_INPUT!r}) = 0x{actual:08X}，"
            f"期望 0x{SELF_CHECK_EXPECTED:08X}（CRC-32/ISO-HDLC 参数漂移）"
        )


def verify_crc(header: bytes, data: bytes) -> None:
    """CRC-32/ISO-HDLC 覆盖帧头偏移 2–21 + 数据区有效字节（§3 冻结）。

    失败抛 FrameRejected('crc')——坏帧整帧丢弃，不污染文件。
    """
    data_len = int.from_bytes(header[13:15], "big")
    expected = int.from_bytes(header[22:26], "big")
    actual = zlib.crc32(header[2:22] + data[:data_len]) & 0xFFFFFFFF
    if actual != expected:
        raise FrameRejected("crc", f"期望 0x{expected:08X}，实际 0x{actual:08X}")
