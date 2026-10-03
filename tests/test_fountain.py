"""实验喷泉码：丢帧恢复、续传及浏览器 PNG 跨端闭环。"""

import base64
import gzip
import random
import zlib
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest

from receiver.fountain import FountainBlock, coefficient_mask, repair_payload
from receiver.metadata import FileMetadata
from receiver.pipeline import DecodedFrame, FrameHeader, decode_frame, parse_header
from receiver.protocol import FLAGS_FOUNTAIN, FLAGS_GZIP, FLAGS_REPAIR, FrameRejected
from receiver.store import FrameStore
from tapemaker.frames import Geometry, build_header


def setup_transfer(store, size=9000):
    plain = random.Random(39).randbytes(size)
    compressed = gzip.compress(plain, mtime=0)
    chunk = 262
    parts = [compressed[i:i + chunk] for i in range(0, len(compressed), chunk)]
    file_id = zlib.crc32(plain) & 0xFFFFFFFF

    def frame(n, payload, repair=False):
        h = FrameHeader(file_id=file_id, frame_no=n, total_frames=len(parts),
                        data_len=len(payload), chunk_size=chunk, cols=48, rows=48,
                        bit=4, pad=3, flags=FLAGS_GZIP | FLAGS_FOUNTAIN
                        | (FLAGS_REPAIR if repair else 0))
        return DecodedFrame(header=h, payload=payload)

    meta = frame(0xFFFFFF, b"")
    meta.metadata = FileMetadata(method=1, plain_size=len(plain),
                                 compressed_size=len(compressed), name="fountain.bin")
    store.add(meta)
    return plain, compressed, parts, frame


@pytest.mark.parametrize("repair_first", [False, True])
def test_recover_many_missing_and_short_last_group(repair_first):
    store = FrameStore()
    plain, compressed, parts, frame = setup_transfer(store)
    groups = (len(parts) + 31) // 32
    missing = {0, 2, 7, 11, 18, len(parts) - 1}
    repairs = []
    for serial in range(45):
        for group in range(groups):
            n = len(parts) + serial * groups + group
            repairs.append(frame(n, repair_payload(parts[group * 32:(group + 1) * 32],
                frame(0, b"").header.file_id, n, 262), True))
    if repair_first:
        for f in repairs:
            store.add(f)
    for n, part in enumerate(parts):
        if n not in missing:
            store.add(frame(n, part))
    if not repair_first:
        for f in repairs:
            store.add(f)
    assert store.is_complete()
    assert store.assemble() == compressed
    assert gzip.decompress(store.assemble()) == plain
    assert not store._fountain_blocks


def test_duplicate_equations_bounded_and_inconsistent_rejected():
    block = FountainBlock(32, 10)
    for _ in range(1000):
        block.add(3, b"a" * 10)
    assert len(block.basis) == 1
    with pytest.raises(FrameRejected, match="不一致"):
        block.add(3, b"b" * 10)


def test_repair_only_late_join_requires_metadata():
    donor = FrameStore()
    plain, compressed, parts, frame = setup_transfer(donor)
    store = FrameStore()
    groups = (len(parts) + 31) // 32
    for serial in range(50):
        for group in range(groups):
            n = len(parts) + serial * groups + group
            store.add(frame(n, repair_payload(parts[group * 32:(group + 1) * 32],
                frame(0, b"").header.file_id, n, 262), True))
    assert store.data_complete()
    assert not store.is_complete()
    meta = frame(0xFFFFFF, b"")
    meta.metadata = donor.metadata
    store.add(meta)
    assert store.assemble() == compressed
    assert gzip.decompress(store.assemble()) == plain


def test_resume_with_new_repairs_and_mode_lock(tmp_path):
    store = FrameStore(tmp_path)
    plain, compressed, parts, frame = setup_transfer(store, 6000)
    for n, part in enumerate(parts):
        if n % 5:
            store.add(frame(n, part))
    store.close()
    resumed = FrameStore(tmp_path)
    for serial in range(40):
        n = len(parts) + serial
        resumed.add(frame(n, repair_payload(parts, frame(0, b"").header.file_id, n, 262), True))
        if resumed.is_complete():
            break
    assert resumed.assemble() == compressed
    old = frame(0, parts[0])
    old.header = replace(old.header, flags=FLAGS_GZIP)
    with pytest.raises(FrameRejected, match="模式"):
        resumed.add(old)
    resumed.close()


def test_version_and_flags_cannot_mix():
    geo = Geometry(48, 48, 4, 3, 216, 216)
    h = build_header(1, 0, 2, 262, geo, FLAGS_GZIP | FLAGS_FOUNTAIN)
    with pytest.raises(FrameRejected, match="v1"):
        parse_header(h)
    h[2] = 2
    assert parse_header(h).flags & FLAGS_FOUNTAIN
    h[20:22] = FLAGS_GZIP.to_bytes(2, "big")
    with pytest.raises(FrameRejected, match="v2"):
        parse_header(h)


def test_coefficient_golden_vector():
    assert [coefficient_mask(0xFFFFFFFF, 0xFFFFFE, n) for n in (1, 17, 32)] == [
        1, 0x170B8, 0xDADD70B8]


def test_browser_png_roundtrip_with_missing_data_and_crc(tmp_path):
    from playwright.sync_api import sync_playwright
    plain = random.Random(7).randbytes(3500)
    source = tmp_path / "browser.bin"
    source.write_bytes(plain)
    store = FrameStore()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 640, "height": 480})
        page.goto((Path(__file__).resolve().parents[1] / "sender.html").as_uri())
        page.click("#modeWindow")
        page.select_option("#coding", "fountain")
        page.fill("#bit", "10")
        page.set_input_files("#file", str(source))
        page.wait_for_selector("body.playing")
        assert page.locator("#coding").is_disabled()
        page.keyboard.press("Escape")
        assert page.locator("#coding").is_enabled()
        assert "接收端确认" in page.locator("#sopTable").inner_text()
        masks = page.evaluate("() => [1,17,32].map(count => "
            "[count, __sender.fountainMask(0xFFFFFFFF, 0xFFFFFE, count)])")
        for count, mask in masks:
            assert coefficient_mask(0xFFFFFFFF, 0xFFFFFE, count) == mask
        files = page.evaluate("""async () => (await __sender.buildExportFrames()).map(f => ({
            name: f.name, text: f.text,
            bytes: f.bytes ? btoa(String.fromCharCode(...f.bytes)) : null
        }))""")
        browser.close()
    lost = {0, 2, 3, 5, 8}
    repairs = 0
    for f in files:
        if not f["bytes"]:
            continue
        img = cv2.imdecode(np.frombuffer(base64.b64decode(f["bytes"]), dtype=np.uint8), 0)
        decoded = decode_frame(img)
        if decoded.header.flags & FLAGS_REPAIR:
            repairs += 1
        if decoded.header.frame_no not in lost:
            store.add(decoded)
    assert repairs >= 18
    assert store.is_complete()
    assert gzip.decompress(store.assemble()) == plain


def test_repair_crc_corruption_rejected(tmp_path):
    from fixture_encoder import render_png
    geo = Geometry(48, 48, 4, 3, 216, 216)
    payload = b"x" * 262
    h = build_header(1, 2, 2, 262, geo, FLAGS_GZIP | FLAGS_FOUNTAIN | FLAGS_REPAIR)
    h[2] = 2
    from receiver.protocol import frame_crc
    h[22:26] = frame_crc(h, payload).to_bytes(4, "big")
    target = tmp_path / "bad.png"
    render_png(bytes(h), b"y" + payload[1:], 4, 3, target)
    with pytest.raises(FrameRejected) as error:
        decode_frame(cv2.imread(str(target), 0))
    assert error.value.reason == "crc"


def test_live_playback_emits_new_repair_ids_after_first_round(tmp_path):
    from playwright.sync_api import sync_playwright
    source = tmp_path / "tiny.bin"
    source.write_bytes(random.Random(1).randbytes(100))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 640, "height": 480})
        page.goto((Path(__file__).resolve().parents[1] / "sender.html").as_uri())
        page.click("#modeWindow")
        page.select_option("#coding", "fountain")
        page.fill("#fps", "30")
        page.set_input_files("#file", str(source))
        page.wait_for_selector("body.playing")
        seen = set()
        for _ in range(12):
            page.wait_for_timeout(80)
            png = page.evaluate("() => document.getElementById('stage').toDataURL().split(',')[1]")
            img = cv2.imdecode(np.frombuffer(base64.b64decode(png), dtype=np.uint8), 0)
            decoded = decode_frame(img)
            if decoded.header.flags & FLAGS_REPAIR:
                seen.add(decoded.header.frame_no)
        assert max(seen) > 3  # tiny 文件 N=1，首轮修复帧号只有 1、2。
        page.wait_for_function("document.title.includes('喷泉码修复')")
        browser.close()
