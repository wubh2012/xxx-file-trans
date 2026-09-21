"""rich 实时进度（需求 F15）。

接收中按任务实时显示帧数 / 百分比 / KB/s / 识别率 / 丢帧，便于判断
信道质量；CRC 连续失败告警限流：连续失败首帧告警一次，此后每
CRC_ALERT_INTERVAL 帧再告警一次，恢复识别时汇报连续失败总数，
避免坏画面逐帧刷屏。rich 在非 tty（子进程管道）下自动降级为普通
文本输出，测试可直接断言终端文本内容。

口径说明：识别率 = 成功解码画面数 / 读入画面总数（含重复帧——无限
重播语义下同一画面会被重复解码，重复帧计入识别成功的分母与分子）；
KB/s 仅累计新收数据帧的有效载荷字节，与进度条 completed 口径一致。
"""

import time
from collections import Counter

from receiver.pipeline import FrameRejected
from rich.progress import (BarColumn, MofNCompleteColumn, Progress,
                           TaskProgressColumn, TextColumn, TimeElapsedColumn)

CRC_ALERT_INTERVAL = 20  # 连续 CRC 失败告警限流间隔（帧）


class ProgressReporter:
    """接收进度上报与显示。

    帧事件经 on_decoded() / on_rejected() 汇入；首帧前总帧数未知，
    set_total() 后切定长进度。finish() 打印收帧汇总行（#6 契约行
    「已收 N/M 帧，丢弃 …」叠加 F15 统计字段），tty 与非 tty 下均为
    可读文本。
    """

    def __init__(self):
        self.total: int | None = None
        self.received = 0  # 新收数据帧数
        self.repeat = 0  # 重复帧 / 元数据帧（无限重播语义，静默）
        self.seen = 0  # 读入画面总数（识别率分母）
        self.payload_bytes = 0  # 有效载荷字节累计（KB/s 分子）
        self.discard_reasons: Counter[str] = Counter()  # 丢弃原因 → 帧数
        self._t0 = time.monotonic()
        self._crc_streak = 0  # 当前连续 CRC 失败长度
        self._crc_total = 0  # CRC 失败帧累计
        self._progress = Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            TextColumn("{task.fields[extra]}"),
        )
        self._task = self._progress.add_task("接收中", total=None, extra="")

    def __enter__(self):
        self._progress.start()
        return self

    def __exit__(self, *exc):
        self._progress.stop()
        return False

    def set_total(self, total: int) -> None:
        """总帧数已知（首帧落地）后切定长进度。"""
        self.total = total
        self._refresh()

    def on_decoded(self, payload: bytes, is_new: bool) -> None:
        """解码成功：新帧计入 KB/s 分子，重复帧静默；CRC 连败就此恢复。"""
        self.seen += 1
        self._close_crc_streak()
        if is_new:
            self.received += 1
            self.payload_bytes += len(payload)
        else:
            self.repeat += 1
        self._refresh()

    def on_rejected(self, name: str, e: FrameRejected) -> None:
        """解码 / 收帧拒绝：CRC 连败限流告警，其余原因逐帧打印。"""
        self.seen += 1
        self.discard_reasons[e.reason] += 1
        if e.reason == "crc":
            self._crc_streak += 1
            self._crc_total += 1
            # 限流：连败首帧必告警，此后每 CRC_ALERT_INTERVAL 帧一次
            if self._crc_streak == 1 or self._crc_streak % CRC_ALERT_INTERVAL == 0:
                self._log(
                    f"告警（crc）：连续 {self._crc_streak} 帧校验失败，"
                    f"累计 {self._crc_total} 帧，限流告警中"
                )
        else:
            self._close_crc_streak()
            self._log(f"丢弃帧 {name}：[{e.reason}] {e.detail}")
        self._refresh()

    def stats_line(self) -> str:
        """收帧汇总行：#6 契约「已收 N/M 帧，丢弃 …」叠加 F15 字段
        （百分比 / 识别率 / KB/s）。"""
        total = self.total if self.total is not None else "?"
        discarded = sum(self.discard_reasons.values())
        rate, kbps = self._metrics()
        line = f"已收 {self.received}/{total} 帧"
        if self.total is not None:
            line += f"（{100.0 * self.received / self.total:.0f}%）"
        if self.discard_reasons:
            line += f"，丢弃 {discarded} 帧（{dict(self.discard_reasons)}）"
        line += f" · 识别率 {rate:.1f}% · {kbps:.1f} KB/s"
        return line

    def finish(self) -> None:
        """结束接收：打印汇总行；未收到任何数据帧时不打印（沿用 #6 行为）。"""
        if self.received > 0:
            self._log(self.stats_line())

    def _metrics(self) -> tuple[float, float]:
        """返回 (识别率 %, KB/s)。识别率口径见模块注释。"""
        elapsed = max(time.monotonic() - self._t0, 1e-9)
        kbps = self.payload_bytes / elapsed / 1024
        rate = 100.0 * (self.seen - sum(self.discard_reasons.values())) / self.seen \
            if self.seen else 100.0
        return rate, kbps

    def _close_crc_streak(self) -> None:
        """CRC 连败中断（后续帧识别成功或因其他原因被拒）时汇报连败总数。"""
        if self._crc_streak:
            self._log(
                f"告警（crc）：连续失败结束，共 {self._crc_streak} 帧后恢复识别"
            )
            self._crc_streak = 0

    def _log(self, text: str) -> None:
        """在进度条不中断的前提下打印一行（关闭标记解析，[reason] 等按字面输出）。"""
        self._progress.console.print(text, markup=False, highlight=False)

    def _refresh(self) -> None:
        rate, kbps = self._metrics()
        discarded = sum(self.discard_reasons.values())
        total = self.total if self.total is not None else "?"
        self._progress.update(
            self._task, completed=self.received, total=self.total,
            extra=(f"丢帧 {discarded} · 识别率 {rate:.1f}% · {kbps:.1f} KB/s · "
                   f"{self.received}/{total}"),
        )
