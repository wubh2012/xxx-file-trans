"""播放器缩放后的传输画面仍按逻辑网格解码，CRC 保持完整校验。"""

import cv2
import argparse
import gzip
import zlib
from pathlib import Path
import numpy as np
import pytest

from receiver.pipeline import FrameRejected, GeometryCache, decode_frame
from receiver.gui_core import ProgressModel
from receiver.cli import receive_frames
from receiver import paths
from tapemaker.frames import build_round, derive_geometry
from tapemaker.render import render_frame


@pytest.mark.parametrize("width", [1280, 1200, 960, 896])
@pytest.mark.parametrize("bit", [8, 15])
def test_player_resize_decodes_original_header_and_payload(width, bit):
    geo = derive_geometry(1920, 1080, bit=bit, pad=3)
    frames = build_round(np.random.default_rng(42).bytes(3000), 123, "scaled.bin", 3000, geo)
    cache = GeometryCache(allow_scaled=True)
    for frame in frames[:3]:
        original = render_frame(frame, geo)
        expected = decode_frame(original)
        scaled = cv2.resize(original, (width, round(width * 1080 / 1920)))
        # 框选包含任意黑色边距，模拟播放器在桌面中的偏移。
        scaled = cv2.copyMakeBorder(scaled, 7, 11, 13, 5, cv2.BORDER_CONSTANT)
        actual = decode_frame(scaled, cache)
        assert actual.header == expected.header
        assert actual.payload == expected.payload


def test_scaled_corruption_is_still_rejected_by_crc():
    geo = derive_geometry(1920, 1080, bit=15, pad=3)
    frame = build_round(np.random.default_rng(42).bytes(3000), 123, "bad.bin", 3000, geo)[1]
    img = render_frame(frame, geo)
    y, x = geo.pad * geo.bit + geo.bit, geo.pad * geo.bit + geo.bit
    img[y:y + geo.bit, x:x + geo.bit] = 255 - img[y:y + geo.bit, x:x + geo.bit]
    scaled = cv2.resize(img, (1200, 675))
    with pytest.raises(FrameRejected) as exc:
        decode_frame(scaled, GeometryCache(allow_scaled=True))
    assert exc.value.reason == "crc"


def test_capture_rejections_are_visible_before_first_data_frame():
    model = ProgressModel()
    model.on_event(("capture_ready",))
    assert "采集已就绪" in model.summary_line()
    model.on_event(("rejected", "geometry", "missing markers"))
    assert "已采集 1 帧" in model.summary_line()
    assert "角标或网格不匹配" in model.summary_line()
    assert "请在发送端开始播放" not in model.summary_line()


@pytest.mark.parametrize("source", ["desktop", "video"])
def test_receive_source_enables_scaled_restore(tmp_path, monkeypatch, source):
    src = np.random.default_rng(42).bytes(3000)
    geo = derive_geometry(1920, 1080, bit=15, pad=3)
    frames = build_round(gzip.compress(src), zlib.crc32(src) & 0xFFFFFFFF,
                         "scaled.bin", len(src), geo)
    monkeypatch.setattr(paths, "PROGRESS_DIR", tmp_path / "progress")
    out = tmp_path / "out"
    stream = ((str(i), cv2.resize(render_frame(frame, geo), (1200, 675)))
              for i, frame in enumerate(frames))
    assert receive_frames(argparse.Namespace(source=source, out=out), stream) == 0
    assert (out / "scaled.bin").read_bytes() == src


@pytest.mark.parametrize("name,frame_no", [("frame_02.png", 88), ("frame_03.png", 125)])
def test_real_bilibili_capture_with_white_margin_and_marker_bridge(name, frame_no):
    """实际 MSS 失败帧：页面白边连角标，转码伪影把右上角标连到数据。"""
    img = cv2.imread(str(Path(__file__).parent / "fixtures/bilibili_capture" / name), 0)
    frame = decode_frame(img, GeometryCache(allow_scaled=True))
    assert frame.header.frame_no == frame_no
    assert frame.header.total_frames == 3756
    assert len(frame.payload) == 950


def test_real_capture_cache_uses_coordinates_in_full_capture():
    cache = GeometryCache(allow_scaled=True)
    root = Path(__file__).parent / "fixtures/bilibili_capture"
    for name, number in [("frame_02.png", 88), ("frame_03.png", 125)]:
        img = cv2.imread(str(root / name), 0)
        assert decode_frame(img, cache).header.frame_no == number


def test_canvas_detection_does_not_accept_obscured_data():
    img = cv2.imread(str(Path(__file__).parent / "fixtures/bilibili_capture/frame_02.png"), 0)
    img[110:600, 620:1200] = 255  # 接收窗口覆盖数据，但不覆盖四个角标
    with pytest.raises(FrameRejected) as exc:
        decode_frame(img, GeometryCache(allow_scaled=True))
    assert exc.value.reason == "crc"
