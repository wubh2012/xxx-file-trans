"""稳定帧闸门（issue #9）：desktop 抓屏与 video 解帧共用的画面电平。

三个判定，按序生效（docs/protocol.md 之外的行为层约定，spec 用户故事 23）：

- **变化检测**：画面与最近一次已放行帧一致（含微幅噪点容差）→ 跳过。
  无限重播语义下同一画面会被反复捕获，放行前拦截，不重复进流水线。
- **稳定两帧判定**：与最近一次放行帧不同的新画面，须连续两次捕获一致
  才放行。RDP 渐进刷新的过渡画面彼此不同，各自把候选顶掉，直到刷新
  完成出现两帧一致才放行——过渡画面永远不会进流水线。
- **超时强制解码**：距上次放行超过 timeout_s 仍有未稳定候选 → 放行
  最新帧。画面永不稳定（持续动画/持续伪影）时不永久卡死，坏帧由
  CRC 兜底整帧丢弃。

帧一致判定带容差：单像素差 ≤ tol 视为相等；超 tol 像素占比 ≤ frac_max
视为同帧。整幅渐变（压缩伪影）与小面积噪声（光标闪烁）由此免疫，
半幅翻转级别的真实换帧不受影响。

now 由调用方传单调时钟时间戳（time.monotonic()），闸门自身不取时间，
测试与 video 源（#10）可注入确定性时钟。
"""

import time

import numpy as np


class StableFrameGate:
    """画面采集流 → 流水线放行决策。feed() 逐帧喂入，返回需放行的帧或 None。"""

    def __init__(
        self,
        stable_count: int = 2,
        timeout_s: float = 5.0,
        tol: int = 2,
        frac_max: float = 5e-4,
    ):
        self.stable_count = stable_count
        self.timeout_s = timeout_s
        self.tol = tol
        self.frac_max = frac_max
        self._last_emitted: np.ndarray | None = None  # 最近放行帧（变化检测基准）
        self._candidate: np.ndarray | None = None  # 未稳定候选画面
        self._candidate_hits = 0  # 候选连续一致的捕获次数
        self._anchor: float | None = None  # 最近一次放行的时刻（超时窗起点）

    def _same(self, a: np.ndarray, b: np.ndarray) -> bool:
        """帧一致判定：形状相同 + 超 tol 像素占比 ≤ frac_max。"""
        if a.shape != b.shape:
            return False
        diff = np.abs(a.astype(np.int16) - b.astype(np.int16))
        return bool((diff > self.tol).mean() <= self.frac_max)

    def feed(self, img: np.ndarray, now: float | None = None) -> np.ndarray | None:
        """喂入一帧采集画面；需向流水线放行时返回该帧，否则返回 None。"""
        if now is None:
            now = time.monotonic()
        if self._anchor is None:
            self._anchor = now

        # 变化检测：与已放行帧一致 → 跳过（含噪点容差）
        if self._last_emitted is not None and self._same(img, self._last_emitted):
            self._candidate = None
            self._candidate_hits = 0
            return None

        # 稳定两帧判定：与候选一致 → 计数；计满放行
        if self._candidate is not None and self._same(img, self._candidate):
            self._candidate_hits += 1
            if self._candidate_hits >= self.stable_count:
                return self._emit(self._candidate, now)
            return None

        # 与候选不同 → 换候选；但若超时窗已满，强制放行最新帧
        if now - self._anchor >= self.timeout_s:
            return self._emit(img, now)
        self._candidate = img
        self._candidate_hits = 1
        return None

    def _emit(self, img: np.ndarray, now: float) -> np.ndarray:
        self._last_emitted = img
        self._candidate = None
        self._candidate_hits = 0
        self._anchor = now
        return img
