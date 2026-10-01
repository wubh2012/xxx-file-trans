"""角标检测缓存（C1）+ 局部采样（C2）（issue #30）：解码提速的行为保持。

C1：GeometryCache 命中时跳过全量连通域检测（bootstrap_geometry 只在
首帧/轻量校验失败时调用）；角标被破坏 / 画面尺寸变化 / 裁切偏移漂移
均须回退全量检测，结果与无缓存路径逐位一致（误命中由帧头交叉校验 +
CRC 兜底，此处用受控场景验证回退路径本身）。

C2：_sample_grid 改按格心取局部 k×k 邻域后，与原全图 boxFilter 参照
实现逐位一致（BIT=1..8 覆盖 k=1..5 全部取值）。
"""

import cv2
import numpy as np
import pytest
import zlib

import fixture_encoder
import receiver.pipeline as pipeline
from receiver.pipeline import (
    FrameRejected,
    GeometryCache,
    _sample_grid,
    bootstrap_geometry,
    decode_frame,
)


def _render_frame(src: bytes, tmp_path, bit: int = 4, pad: int = 3,
                  cols: int = 48, rows: int = 48) -> np.ndarray:
    comp = len(src) and src
    chunk = fixture_encoder.frame_capacity(cols, rows)
    header = fixture_encoder.signed_header(
        zlib.crc32(b"x") & 0xFFFFFFFF, 0, 1, comp[:chunk], chunk, cols, rows, bit, pad)
    p = tmp_path / "frame.png"
    fixture_encoder.render_png(header, comp[:chunk], bit, pad, p)
    img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
    assert img is not None
    return img


def _embed(img: np.ndarray, top: int, left: int) -> np.ndarray:
    """帧画面嵌入更大的黑背景（模拟框选矩形比画布大一圈），偏移任意。"""
    out = np.zeros((img.shape[0] + top + 11, img.shape[1] + left + 5), dtype=np.uint8)
    out[top:top + img.shape[0], left:left + img.shape[1]] = img
    return out


def _spy_bootstrap(monkeypatch) -> list[int]:
    """统计 bootstrap_geometry 调用次数（缓存命中判据）。"""
    calls = []
    real = pipeline.bootstrap_geometry

    def spy(bw):
        calls.append(1)
        return real(bw)

    monkeypatch.setattr(pipeline, "bootstrap_geometry", spy)
    return calls


# ---------- C1：缓存命中与回退 ----------


def test_cache_hit_skips_full_detection(tmp_path, monkeypatch):
    """同几何连续帧只做一次全量检测，结果与无缓存路径一致。"""
    img_a = _render_frame(b"\xA5" * 64, tmp_path)
    img_b = _render_frame(b"\x5A" * 64, tmp_path)  # 同几何不同内容
    ref_a, ref_b = decode_frame(img_a), decode_frame(img_b)  # 参照先行，不掺入计数

    calls = _spy_bootstrap(monkeypatch)
    cache = GeometryCache()
    assert decode_frame(img_a, cache) == ref_a
    assert decode_frame(img_b, cache) == ref_b
    assert len(calls) == 1  # 第二帧命中缓存


def test_validated_cache_decodes_when_only_marker_destroyed(tmp_path, monkeypatch):
    """已有验证几何时仅遮挡角标，数据区仍通过完整帧头和 CRC 校验。"""
    calls = _spy_bootstrap(monkeypatch)
    img = _render_frame(b"\xA5" * 64, tmp_path)
    cache = GeometryCache()
    decode_frame(img, cache)
    assert len(calls) == 1

    bit, pad = 4, 3
    broken = img.copy()
    broken[pad * bit - 3 * bit:pad * bit, pad * bit - 3 * bit:pad * bit] = 0  # 涂黑左上角标
    assert decode_frame(broken, cache) == decode_frame(img)
    assert len(calls) == 2  # 无缓存参照额外检测一次

    decode_frame(img, cache)  # 好帧恢复：轻量校验通过，命中缓存
    assert len(calls) == 2


def test_cached_geometry_rejects_marker_and_data_occlusion(tmp_path):
    img = _render_frame(b"\xA5" * 64, tmp_path)
    cache = GeometryCache()
    decode_frame(img, cache)
    broken = img.copy()
    broken[:12, :12] = 0
    broken[28:32, 28:32] = 255 - broken[28:32, 28:32]
    with pytest.raises(FrameRejected):
        decode_frame(broken, cache)


def test_quick_threshold_falls_back_for_low_contrast(tmp_path):
    img = _render_frame(b"\xA5" * 64, tmp_path)
    cache = GeometryCache()
    expected = decode_frame(img, cache)
    dim = np.where(img == 0, 20, 100).astype(np.uint8)
    assert decode_frame(dim, cache) == expected


def test_failed_first_crc_does_not_validate_geometry(tmp_path):
    img = _render_frame(b"\xA5" * 64, tmp_path)
    img[28:32, 28:32] = 255 - img[28:32, 28:32]
    cache = GeometryCache()
    with pytest.raises(FrameRejected):
        decode_frame(img, cache)
    assert not cache._validated


def test_cache_falls_back_on_shape_change(tmp_path):
    """画面尺寸变化（裁切边距改变形状）→ 回退全量检测，解码不受影响。"""
    img = _render_frame(b"\xA5" * 64, tmp_path)
    ref = decode_frame(img)

    cache = GeometryCache()
    assert decode_frame(img, cache) == ref
    cropped = decode_frame(_embed(img, top=7, left=13), cache)
    assert cropped.header == ref.header
    assert cropped.payload == ref.payload


def test_cache_falls_back_on_offset_shift_same_shape(tmp_path):
    """同尺寸不同裁切偏移（轻量校验必须探得到角标位移，不能只比 shape）。"""
    img = _render_frame(b"\xA5" * 64, tmp_path)
    h, w = img.shape
    canvas_a = np.zeros((h + 40, w + 40), dtype=np.uint8)
    canvas_b = np.zeros((h + 40, w + 40), dtype=np.uint8)
    canvas_a[9:9 + h, 9:9 + w] = img
    canvas_b[25:25 + h, 25:25 + w] = img

    ref = decode_frame(canvas_a)
    cache = GeometryCache()
    assert decode_frame(canvas_a, cache) == ref
    out = decode_frame(canvas_b, cache)  # 同 shape，角标位置已漂移
    assert out.header == ref.header
    assert out.payload == ref.payload


# ---------- C2：局部采样等价性 ----------


def _sample_grid_reference(bw, geo, rect) -> bytes:
    """旧实现参照：全图 boxFilter 后取格心（issue #30 改造前）。"""
    x0, y0, _, _ = rect
    k = min(5, geo.bit)
    if k % 2 == 0:
        k -= 1
    mean = cv2.boxFilter(bw, ddepth=-1, ksize=(k, k), borderType=cv2.BORDER_REPLICATE)
    centers_y = (y0 + (np.arange(geo.rows) + 0.5) * geo.bit).astype(int)
    centers_x = (x0 + (np.arange(geo.cols) + 0.5) * geo.bit).astype(int)
    window = mean[np.ix_(centers_y, centers_x)]
    bits = (window < 128).astype(np.uint8)
    return np.packbits(bits.flatten(), bitorder="big").tobytes()


@pytest.mark.parametrize("bit", [1, 2, 3, 4, 5, 6, 7, 8])
def test_local_sampling_equals_boxfilter_reference(tmp_path, bit):
    """局部采样与全图 boxFilter 逐位一致（bw 为 0/255，均值即暗多数表决，
    不存在取整边界歧义）；BIT=1..8 覆盖 k=1/3/5 全部取值。"""
    payload = bytes((i * 37 + 11) % 256 for i in range(64))
    img = _render_frame(payload, tmp_path, bit=bit)
    _, bw = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    geo, rect = bootstrap_geometry(bw)
    assert _sample_grid(bw, geo, rect) == _sample_grid_reference(bw, geo, rect)
