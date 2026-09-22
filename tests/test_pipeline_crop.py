"""几何自举裁切容忍（issue #22）：PAD 枚举 + 帧头交叉校验 + CRC 仲裁。

区域框选（--region pick）只要求把帧画布完整框进采集区域，框选矩形
与画布之间允许任意边距（含非 BIT 整数倍）。旧实现按「画面即画布」
从网格原点推 PAD，任意左边距/上边距即整帧被拒；现升级为角标定位
网格矩形后枚举 PAD 候选，帧头声明裁定，CRC 兜底。线缆协议零改动。
"""

import cv2
import numpy as np
import pytest
import zlib

import fixture_encoder
from receiver.pipeline import FrameRejected, decode_frame


def _render_frame(src: bytes, tmp_path, bit: int = 4, pad: int = 3,
                  cols: int = 48, rows: int = 48) -> np.ndarray:
    """夹具编码一帧数据帧（元数据帧同样适用，此处用数据帧足够）。"""
    comp = len(src) and src  # 直接以给定字节为数据区
    chunk = fixture_encoder.frame_capacity(cols, rows)
    header = fixture_encoder.signed_header(
        zlib.crc32(b"x") & 0xFFFFFFFF, 0, 1, comp[:chunk], chunk, cols, rows, bit, pad)
    p = tmp_path / "frame.png"
    fixture_encoder.render_png(header, comp[:chunk], bit, pad, p)
    img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
    assert img is not None
    return img


def _crop_with_margin(img: np.ndarray, top: int, left: int) -> np.ndarray:
    """帧画面嵌入更大的黑背景（模拟框选矩形比画布大一圈），偏移任意。"""
    out = np.zeros((img.shape[0] + top + 11, img.shape[1] + left + 5), dtype=np.uint8)
    out[top:top + img.shape[0], left:left + img.shape[1]] = img
    return out


def test_decode_tolerates_non_bit_multiple_margin(tmp_path):
    """任意裁切边距（非 BIT 整数倍：top=7 / left=13）仍正确解码。"""
    img = _render_frame(b"\xA5" * 64, tmp_path)
    h = decode_frame(img).header
    cropped = decode_frame(_crop_with_margin(img, top=7, left=13))
    assert cropped.header == h
    assert cropped.payload


def test_decode_tolerates_zero_and_large_margin(tmp_path):
    """贴边（0 边距，旧行为）与大边距（框大很多）两端都容忍。"""
    img = _render_frame(b"\x5A" * 64, tmp_path)
    h = decode_frame(img).header
    assert decode_frame(_crop_with_margin(img, top=0, left=0)).header == h
    assert decode_frame(_crop_with_margin(img, top=40, left=25)).header == h


def test_crop_bad_crc_keeps_precise_reason(tmp_path):
    """裁切输入下诊断精度保留：几何一致但 CRC 损坏 → 拒因仍是 crc，
    不退化成笼统的几何拒绝（test_desktop_source 的 CRC 拒绝路径依赖）。"""
    img = _render_frame(b"\x33" * 64, tmp_path)
    bad = img.copy()
    # 翻转数据区内 2×2 个方块（面积超闸门容差、破坏 CRC、不碰角标）
    bit, pad = 4, 3
    oy, ox = pad * bit + bit, pad * bit + bit
    bad[oy:oy + 2 * bit, ox:ox + 2 * bit] = 255 - bad[oy:oy + 2 * bit, ox:ox + 2 * bit]
    cropped = _crop_with_margin(bad, top=9, left=6)
    with pytest.raises(FrameRejected) as e:
        decode_frame(cropped)
    assert e.value.reason == "crc"


def test_crop_missing_markers_still_rejected(tmp_path):
    """裁切容忍不放松角标校验：角标被裁掉的帧整帧拒绝。"""
    img = _render_frame(b"\x99" * 64, tmp_path)
    beheaded = img[6:, :]  # 裁掉顶部：左上/右上角标残缺
    with pytest.raises(FrameRejected) as e:
        decode_frame(beheaded)
    assert e.value.reason == "geometry"
