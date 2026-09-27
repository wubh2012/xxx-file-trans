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
FLAGS_GZIP = 0x8000
FLAGS_FEC = 0x4000
FLAGS_ALLOWED = FLAGS_GZIP | FLAGS_FEC

# 前向纠错参数（协议 §8）：每 32 个数据帧附带 2 个校验帧，可恢复同组
# 最多 2 个缺失数据帧。参数固定在协议中，避免再占用已冻结帧头字段。
FEC_GROUP_SIZE = 32
FEC_PARITY_FRAMES = 2


def fec_parity_count(total_frames: int) -> int:
    """返回一轮播放需要的 FEC 校验帧数。"""
    if total_frames <= 0:
        return 0
    groups = (total_frames + FEC_GROUP_SIZE - 1) // FEC_GROUP_SIZE
    return groups * FEC_PARITY_FRAMES


def is_fec_frame(frame_no: int, total_frames: int) -> bool:
    """帧号是否落在当前文件的校验帧编号区间。"""
    return total_frames <= frame_no < total_frames + fec_parity_count(total_frames)


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
