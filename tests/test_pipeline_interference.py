"""角标四元组矩形约束选取（issue #37）：整屏采集抗桌面白色元素干扰。

旧实现对四个画面角独立取「切比雪夫距离最近」的实心白方块候选——
该断言只在「画面即画布」时成立；整屏采集时画布外的桌面白色元素
（或画布内恰好构成合法矩形的数据白块）可抢占角标候选，几何自举
整帧拒绝。现改为全局搜索满足矩形约束的四元组（同 side 配对、
轴对齐、网格整倍数剪枝、多解取覆盖范围最大者），真角标内角四点
构成严格矩形而随机白块四人成组概率趋近于零（ADR-0001 测量模型
不变，改的只是候选选取）。
"""

import time
import zlib

import cv2
import numpy as np
import pytest

import fixture_encoder
from receiver.pipeline import (
    FrameRejected,
    bootstrap_geometry,
    decode_frame,
    measure_geometry,
)

BIT, PAD, COLS, ROWS = 6, 3, 48, 48  # issue #37 实测场景：BIT=6，数据白块 2×BIT=12px


def _render_frame(tmp_path, src: bytes | None = None) -> np.ndarray:
    """BIT=6 / PAD=3 数据帧。src 缺省取满容量 0xFF：数据网格全黑，
    只剩帧头 0 bit 的孤立白块，便于按像素精确布置干扰结构。"""
    chunk = fixture_encoder.frame_capacity(COLS, ROWS)
    comp = src if src is not None else b"\xFF" * chunk
    header = fixture_encoder.signed_header(
        zlib.crc32(b"x") & 0xFFFFFFFF, 0, 1, comp[:chunk], chunk, COLS, ROWS, BIT, PAD)
    p = tmp_path / "frame.png"
    fixture_encoder.render_png(header, comp[:chunk], BIT, PAD, p)
    img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
    assert img is not None
    return img


def _embed_fullscreen(img: np.ndarray, margin: int = 100) -> np.ndarray:
    """帧画布嵌入 (40, 40) 偏移的黑背景（模拟整屏采集：画布居中、
    四周留出桌面区域）。"""
    out = np.zeros((img.shape[0] + margin, img.shape[1] + margin), dtype=np.uint8)
    out[40:40 + img.shape[0], 40:40 + img.shape[1]] = img
    return out


def _paint_white(img: np.ndarray, left: int, top: int, side: int) -> None:
    img[top:top + side, left:left + side] = 255


def test_br_decoy_between_canvas_and_screen_corner(tmp_path):
    """回归（本 issue）：画布 BR 外侧、更贴屏幕角的 12px 白色干扰块
    抢占就近候选 → 旧实现「四角标边长不一致 [18,18,18,12]」整帧拒绝；
    新实现矩形搜索锁定真四元组，照常解码。"""
    ref = decode_frame(_embed_fullscreen(_render_frame(tmp_path)))

    img = _embed_fullscreen(_render_frame(tmp_path))
    ch, cw = 324, 324  # (48 + 2×3) × BIT=6
    _paint_white(img, 40 + cw + 6, 40 + ch + 6, 2 * BIT)  # 干扰块距屏幕角 22px < 真角标 40px

    out = decode_frame(img)
    assert out.header == ref.header
    assert out.payload == ref.payload


def test_inner_fake_quadruple_loses_to_outer_true(tmp_path):
    """多四元组裁决：网格内四个孤立数据白块恰好构成合法小矩形四元组，
    必须选中外沿真矩形（假矩形严格落在真角标矩形内部）。"""
    img = _embed_fullscreen(_render_frame(tmp_path))
    ref_geo, ref_rect = measure_geometry(
        cv2.threshold(img, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1])

    # 画布内网格坐标 (cell 10,10)/(10,30)/(30,10)/(30,30) 处各涂一个
    # 2×2 格白块：四人成组满足全部对齐与整倍数约束（gw=gh=108=18×BIT）
    ox = oy = PAD * BIT
    for r, c in ((10, 10), (10, 30), (30, 10), (30, 30)):
        _paint_white(img, ox + c * BIT, oy + r * BIT, 2 * BIT)

    geo, rect = measure_geometry(
        cv2.threshold(img, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1])
    assert (geo.bit, geo.cols, geo.rows) == (ref_geo.bit, ref_geo.cols, ref_geo.rows)
    assert rect == ref_rect


def test_outer_fake_quadruple_rejected_not_misdecoded(tmp_path):
    """画布外围四个白色元素碰巧构成更大的合法矩形四元组：几何被其
    锚定后下游帧头交叉校验 / CRC 兜底整帧拒绝——宁可拒绝、不出错数据。"""
    img = _embed_fullscreen(_render_frame(tmp_path))
    ch, cw = 324, 324
    # 外围四元组：side=18、gw=gh=360=60×BIT（合法但覆盖范围大于真矩形）
    for left, top in ((10, 10), (10 + 378, 10), (10, 10 + 378), (10 + 378, 10 + 378)):
        _paint_white(img, left, top, 3 * BIT)

    with pytest.raises(FrameRejected):
        decode_frame(img)


def test_random_noise_frame_bounded_time():
    """性能护栏：满分辨率随机噪声帧（候选可达数千）上几何测量有界，
    防配对搜索退化成 O(n²) 全组合。"""
    rng = np.random.default_rng(42)
    img = rng.integers(0, 256, (1080, 1920), dtype=np.uint8)
    bw = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]
    start = time.perf_counter()
    try:
        measure_geometry(bw)
    except FrameRejected:
        pass  # 噪声帧无有效四元组属正常，计时不受影响
    assert time.perf_counter() - start < 1.0


def test_bootstrap_still_rejects_without_any_square(tmp_path):
    """无任何实心方块候选（纯黑画面）维持几何拒绝，拒因不变。"""
    with pytest.raises(FrameRejected) as e:
        bootstrap_geometry(np.zeros((100, 100), dtype=np.uint8))
    assert e.value.reason == "geometry"
