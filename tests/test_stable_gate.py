"""稳定帧闸门 StableFrameGate（issue #9）。

desktop 抓屏 / video 解帧的共用电平：变化检测（与已放行帧相同则跳过，
无限重播不重复解码）+ 稳定两帧判定（连续两帧一致才放行，适配 RDP
渐进刷新的过渡画面）+ 超时强制解码（画面长时间无法稳定时放行最新帧，
让 CRC 兜底，避免永不放行卡死）。后续 video 源（#10）共用本闸门。

now 由调用方显式传入（单调时钟），测试完全确定性。
"""

import numpy as np
import pytest

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


def test_high_rate_unique_frames_are_forced_through_short_window():
    """高 FPS 抓屏每次可能只采到一个新画面，桌面源的 80ms 窗口不能再
    退化成旧实现的 5 秒一次放行。CRC 会在后续流水线拦截过渡坏帧，闸门
    只负责保证候选不会长时间饿死。"""
    g = StableFrameGate(timeout_s=0.08)
    rng = np.random.default_rng(123)
    emitted = []
    for i in range(30):  # 1 秒、30 FPS，每次画面都不同
        out = g.feed(rng.integers(0, 256, (16, 16), dtype=np.uint8), now=i / 30)
        if out is not None:
            emitted.append(i)
    assert emitted, "唯一新画面不应等到 5 秒超时才首次放行"
    assert max(b - a for a, b in zip(emitted, emitted[1:])) <= 4, \
        "短窗口应持续放行候选，而非每 5 秒跳一次"


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


def test_flush_emits_pending_candidate():
    """有限流结束（video 源 #10）：未稳定候选（末帧首现一次、录制恰好在
    此截尾）flush 放行，不静默丢失。"""
    g = StableFrameGate()
    a, b = img(1), img(2)
    assert g.feed(a, now=0.0) is None
    assert g.feed(a, now=0.1) is not None  # A 稳定放行
    assert g.feed(b, now=0.2) is None  # B 首现入候选，流在此结束
    out = g.flush()
    assert out is not None and np.array_equal(out, b)


def test_flush_without_pending_candidate():
    """无未稳定候选（从未喂帧 / 候选已放行清空）→ flush 返回 None。"""
    g = StableFrameGate()
    assert g.flush() is None
    a = img(1)
    assert g.feed(a, now=0.0) is None
    assert g.feed(a, now=0.1) is not None  # A 稳定放行，候选已清空
    assert g.flush() is None


@pytest.mark.parametrize("tol", [0, 2, 2.5, 255])
@pytest.mark.parametrize("frac_max", [0, 5e-4, 0.25, 1])
def test_comparison_matches_original_at_noise_boundaries(tol, frac_max):
    """像素差和噪点比例的边界不因 uint8 快速路径而改变。"""
    g = StableFrameGate(tol=tol, frac_max=frac_max)
    a = np.zeros((40, 50), dtype=np.uint8)
    for count in (0, 1, 2, 499, 500, 501, 2000):
        b = a.copy()
        b.flat[:count] = 3
        b.flat[0] = 255  # 无符号差值不得回绕
        for left, right in ((a, b), (b, a)):
            expected = bool((np.abs(left.astype(np.int16)
                                    - right.astype(np.int16)) > tol).mean() <= frac_max)
            assert g._same(left, right) == expected


def test_comparison_strided_frames_and_buffer_resize():
    """裁切视图、区域尺寸切换与缓冲区复用应保持逐像素一致。"""
    g = StableFrameGate()
    for size in (32, 64, 32):
        a = img(7, size)[::2, ::2]
        b = a.copy()
        assert g._same(a, b)
        diff_buffer = g._diff
        b[0, 0] = 255 - b[0, 0]
        assert not g._same(a, b)
        assert g._diff is diff_buffer


def test_comparison_non_uint8_preserves_original_behavior():
    g = StableFrameGate()
    a = img(1).astype(np.int16)
    assert g._same(a, a.copy())
    assert not g._same(a, 255 - a)
