"""实验 v2：32 分片 GF(2) 随机线性喷泉码，非 LT/RaptorQ。"""

from receiver.protocol import FEC_GROUP_SIZE


def repair_coordinates(frame_no: int, total: int) -> tuple[int, int]:
    groups = (total + FEC_GROUP_SIZE - 1) // FEC_GROUP_SIZE
    serial, group = divmod(frame_no - total, groups)
    return group, serial


def coefficient_mask(file_id: int, frame_no: int, count: int) -> int:
    x = (file_id ^ ((frame_no + 1) * 0x9E3779B1) ^ 0xA5A5A5A5) & 0xFFFFFFFF
    # MurmurHash3 finalizer：仅 xorshift 对连续序号可能产生秩不足。
    x ^= x >> 16
    x = (x * 0x85EBCA6B) & 0xFFFFFFFF
    x ^= x >> 13
    x = (x * 0xC2B2AE35) & 0xFFFFFFFF
    x ^= x >> 16
    return (x & ((1 << count) - 1)) or 1


def repair_payload(parts: list[bytes], file_id: int, frame_no: int, chunk: int) -> bytes:
    mask = coefficient_mask(file_id, frame_no, len(parts))
    value = 0
    for i, part in enumerate(parts):
        if mask & (1 << i):
            value ^= int.from_bytes(part.ljust(chunk, b"\x00"), "little")
    return value.to_bytes(chunk, "little")


class FountainBlock:
    """增量约化消元；独立方程数量至多为分片数，与修复帧数无关。"""

    def __init__(self, count: int, chunk: int):
        self.count = count
        self.chunk = chunk
        self.basis: dict[int, tuple[int, int]] = {}
        self.emitted: set[int] = set()

    def add(self, mask: int, payload: bytes) -> list[tuple[int, bytes]]:
        value = int.from_bytes(payload.ljust(self.chunk, b"\x00"), "little")
        for pivot in sorted(self.basis):
            if mask & (1 << pivot):
                row, rhs = self.basis[pivot]
                mask ^= row
                value ^= rhs
        if not mask:
            if value:
                from receiver.protocol import FrameRejected
                raise FrameRejected("fountain", "相关方程内容不一致")
            return []
        pivot = (mask & -mask).bit_length() - 1
        for old_pivot, (row, rhs) in list(self.basis.items()):
            if row & (1 << pivot):
                self.basis[old_pivot] = (row ^ mask, rhs ^ value)
        self.basis[pivot] = (mask, value)
        solved = []
        for index, (row, rhs) in self.basis.items():
            if row == 1 << index and index not in self.emitted:
                self.emitted.add(index)
                solved.append((index, rhs.to_bytes(self.chunk, "little")))
        return solved
