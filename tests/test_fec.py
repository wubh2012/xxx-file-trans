"""前向纠错分组恢复测试。

用两个缺失数据帧验证接收端可以只凭校验帧恢复 gzip 分片；三帧缺失时
仍保持未完成，不把无法证明完整的数据交给还原层。
"""

import gzip
import random
import zlib

from receiver.fec import gf_mul
from receiver.pipeline import DecodedFrame, FrameHeader
from receiver.protocol import (
    FEC_GROUP_SIZE,
    FEC_PARITY_FRAMES,
    FLAGS_FEC,
    FLAGS_GZIP,
    FRAME_NO_METADATA,
)
from receiver.store import FrameStore
from receiver.metadata import FileMetadata


def _header(file_id, frame_no, total, chunk, flags):
    return FrameHeader(file_id=file_id, frame_no=frame_no, total_frames=total,
                       data_len=chunk, chunk_size=chunk, cols=48, rows=48,
                       bit=4, pad=3, flags=flags)


def _parity(parts: list[bytes], parity_index: int, chunk: int) -> bytes:
    out = bytearray(chunk)
    for relative, part in enumerate(parts):
        coeff = relative + 1
        for i, value in enumerate(part):
            out[i] ^= value if parity_index == 0 else gf_mul(coeff, value)
    return bytes(out)


def _store_with_missing(missing: set[int], *, parity_first: bool = False) -> tuple[FrameStore, bytes, int]:
    plain = random.Random(42).randbytes(5000)
    compressed = gzip.compress(plain)
    chunk = 128
    total = (len(compressed) + chunk - 1) // chunk
    file_id = zlib.crc32(plain) & 0xFFFFFFFF
    store = FrameStore()
    store.add(DecodedFrame(
        header=_header(file_id, FRAME_NO_METADATA, total, chunk, FLAGS_GZIP),
        payload=b"", metadata=FileMetadata(
            method=1, plain_size=len(plain),
            compressed_size=len(compressed), name="fec.bin")))
    parts = [compressed[i * chunk:(i + 1) * chunk].ljust(chunk, b"\x00")
             for i in range(total)]
    # 只测第一组，缺失帧全部安排在同组内。
    group = parts[:min(FEC_GROUP_SIZE, total)]
    def add_parity():
        for parity in range(FEC_PARITY_FRAMES):
            parity_no = total + parity
            store.add(DecodedFrame(
                header=_header(file_id, parity_no, total, chunk, FLAGS_GZIP | FLAGS_FEC),
                payload=_parity(group, parity, chunk)))
    if parity_first:
        add_parity()
    for n, part in enumerate(parts):
        if n not in missing:
            store.add(DecodedFrame(
                header=_header(file_id, n, total, chunk, FLAGS_GZIP),
                payload=part))
    if not parity_first:
        add_parity()
    return store, plain, total


def test_two_missing_frames_recovered_and_assembled():
    store, plain, _total = _store_with_missing({3, 17})
    assert store.data_complete()
    assert gzip.decompress(store.assemble()) == plain


def test_three_missing_frames_remain_incomplete():
    store, _plain, _total = _store_with_missing({3, 17, 21})
    assert not store.data_complete()


def test_data_arriving_after_parity_still_triggers_recovery():
    store, plain, _total = _store_with_missing({3, 17}, parity_first=True)
    assert store.data_complete()
    assert gzip.decompress(store.assemble()) == plain
