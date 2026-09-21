"""稳定帧闸门 StableFrameGate（issue #9）。

desktop 抓屏 / video 解帧的共用电平：变化检测（与已放行帧相同则跳过，
无限重播不重复解码）+ 稳定两帧判定（连续两帧一致才放行，适配 RDP
渐进刷新的过渡画面）+ 超时强制解码（画面长时间无法稳定时放行最新帧，
让 CRC 兜底，避免永不放行卡死）。后续 video 源（#10）共用本闸门。

now 由调用方显式传入（单调时钟），测试完全确定性。
"""

import numpy as np

from receiver.sources.stable import StableFrameGate


def img(seed: int, size: int = 32) -> np.ndarray:
    """确定性灰度测试画面。"""
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=(size, size), dtype=np.uint8)


def noisy(base: np.ndarray, n_px: int = 4, seed: int = 99) -> np.ndarray:
    """在 base 上制造 n_px 个微幅（≤ tol）扰动的画面：闸门应视为同帧。"""
    out = base.copy()
    rng = np.random.default_rng(seed)
    for _ in range(n_px):
        y, x = rng.integers(0, out.shape[0]), rng.integers(0, out.shape[1])
        out[y, x] = np.clip(int(out[y, x]) + rng.integers(1, 3), 0, 255)
    return out


def test_stable_two_frames_emitted_once():
    """连续两帧一致 → 第二次放行该帧；后续同帧重复捕获（无限重播）不再放行。"""
    g = StableFrameGate()
    a = img(1)
    assert g.feed(a, now=0.0) is None  # 首帧入候选
    out = g.feed(a, now=0.1)
    assert out is not None and np.array_equal(out, a)  # 稳定两帧 → 放行
    for t in (0.2, 0.3, 0.4):
        assert g.feed(a, now=t) is None  # 变化检测：与已放行帧相同 → 跳过


def test_rdp_progressive_refresh():
    """RDP 渐进刷新：过渡画面彼此不同、与最终画面也不同，稳定后才放行最终帧。"""
    g = StableFrameGate()
    a = img(1)
    b = img(2)
    assert g.feed(a, now=0.0) is None
    assert g.feed(a, now=0.1) is not None  # A 稳定放行
    partial1, partial2 = img(7), img(8)
    assert g.feed(partial1, now=0.2) is None  # 过渡画面：入候选
    assert g.feed(partial2, now=0.3) is None  # 又一过渡画面：替换候选，不稳定
    assert g.feed(b, now=0.4) is None  # 最终帧首现：入候选
    out = g.feed(b, now=0.5)
    assert out is not None and np.array_equal(out, b)  # 稳定两帧 → 放行 B


def test_timeout_forced_decode():
    """画面持续不稳定超过超时窗 → 强制放行最新帧，不永久卡死。"""
    g = StableFrameGate(timeout_s=1.0)
    g.feed(img(1), now=0.0)  # 候选 1
    assert g.feed(img(2), now=0.5) is None  # 换画面，窗内不强制
    out = g.feed(img(3), now=1.1)  # 距上次放行 ≥ 超时窗 → 强制放行最新帧
    assert out is not None and np.array_equal(out, img(3))
    # 强制放行后重置计时，重播的强制帧（变化检测）不再放行
    assert g.feed(img(3), now=1.5) is None


def test_timeout_not_fired_within_window():
    """持续不稳定但仍在超时窗内 → 不放行。"""
    g = StableFrameGate(timeout_s=10.0)
    g.feed(img(1), now=0.0)
    assert g.feed(img(2), now=5.0) is None
    assert g.feed(img(3), now=9.9) is None


def test_small_noise_treated_as_same_frame():
    """微幅噪点（幅度 ≤ tol 的少量像素）视为同帧：稳定判定与变化检测都免疫。"""
    g = StableFrameGate()
    a = img(1)
    n1 = noisy(a)
    assert g.feed(a, now=0.0) is None
    assert g.feed(n1, now=0.1) is not None  # 噪点帧与首帧「一致」→ 稳定放行
    assert g.feed(noisy(a, seed=7), now=0.2) is None  # 变化检测同样视为同帧


def test_noise_above_threshold_is_new_frame():
    """噪点超过容差（像素数或幅度）即视为不同帧，走稳定判定。"""
    g = StableFrameGate()
    a = img(1)
    g.feed(a, now=0.0)
    g.feed(a, now=0.1)  # A 放行
    heavy = a.copy()
    heavy[:, : 16] = 255 - heavy[:, :16]  # 半幅翻转：远超 frac 容差
    assert g.feed(heavy, now=0.2) is None  # 不同帧 → 入候选
    assert g.feed(heavy, now=0.3) is not None  # 稳定两帧 → 放行


def test_size_change_is_new_frame():
    """分辨率变化（--region 窗口改变等）视为不同帧。"""
    g = StableFrameGate()
    g.feed(img(1, 32), now=0.0)
    g.feed(img(1, 32), now=0.1)
    assert g.feed(img(1, 64), now=0.2) is None  # 形状不同 → 新候选，不放行也不跳过
