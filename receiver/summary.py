"""还原统计摘要（issue #25）：友好大小 / 耗时 / 明文口径速率 / 计时器。

计时口径（grilling 共识 2026-09-23）：从首个被 FrameStore 接受的新数据帧
落地（store.add() 返回 is_new=True，与参数锁定同点）起算，到文件写盘完成；
首帧落地前的时间（框选 / 等待发送端）不计入。大小为二进制口径 MiB/KiB
（Windows 习惯）；速率为明文大小 / 耗时、与大小同单位，与进度条的压缩
口径 KB/s 并存、各注明口径不混用。
"""

import time


def _scale(n: int) -> tuple[float, str]:
    """二进制口径单位选择：≥ 1 MiB 用 MiB，否则退到 KiB。"""
    if n >= 1024 * 1024:
        return n / (1024 * 1024), "MiB"
    return n / 1024, "KiB"


def friendly_size(n: int) -> str:
    """字节数 → 二进制口径友好大小：≥ 1 MiB 用 MiB，否则退到 KiB。"""
    value, unit = _scale(n)
    return f"{value:.1f} {unit}"


def format_rate(n: int, elapsed: float) -> str:
    """明文口径速率：与 friendly_size 同单位 / 秒（3.5 MiB、42.7 s → 0.1 MiB/s）。"""
    value, unit = _scale(n)
    return f"{value / max(elapsed, 1e-9):.1f} {unit}/s"


def format_elapsed(seconds: float) -> str:
    """耗时格式：秒（1 位小数）。"""
    return f"{seconds:.1f} s"


class RestoreTimer:
    """还原计时器：锚点 = 首个新数据帧落地（start 幂等，不重置）。"""

    def __init__(self):
        self._t0: float | None = None

    @property
    def started(self) -> bool:
        return self._t0 is not None

    def start(self) -> None:
        if self._t0 is None:
            self._t0 = time.monotonic()

    @property
    def elapsed(self) -> float:
        if self._t0 is None:
            return 0.0
        return time.monotonic() - self._t0


def restore_summary(name: str, size: int, elapsed: float, received: int,
                    total: int | None, sha256_hex: str) -> str:
    """成功摘要行（issue #25）：
    还原完成：report.pdf（3.5 MiB · 耗时 42.7 s · 速率 0.1 MiB/s · 帧 120/120 · sha256 ab12cd34…）
    """
    total_s = "?" if total is None else str(total)
    return (f"还原完成：{name}（{friendly_size(size)} · "
            f"耗时 {format_elapsed(elapsed)} · 速率 {format_rate(size, elapsed)} · "
            f"帧 {received}/{total_s} · sha256 {sha256_hex[:8]}…）")


def incomplete_summary(elapsed: float, received: int, total: int | None) -> str:
    """失败 / 中断路径的未完成统计行（issue #25）：已耗时 + 已收 N/M 帧。"""
    total_s = "?" if total is None else str(total)
    return f"还原未完成：已耗时 {format_elapsed(elapsed)} · 已收 {received}/{total_s} 帧"
