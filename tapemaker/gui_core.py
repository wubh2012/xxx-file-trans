"""制片 GUI 无头逻辑层（issue #46）：校验 / 默认输出 / 命令文案 / 制片线程。

缝（tkinter 薄壳 tapemaker_gui.pyw 只装配控件，状态与文案全在此模块
无头测试，同 receiver.gui_core「纯函数拆出 + 薄壳人工验收」惯例）：
- default_output：源文件 → 默认输出 MP4（同目录同名）；
- validate_config：开始前参数校验文案；
- command_line：GUI 参数 → 与 CLI 等效的完整命令文本（命令助手）；
- MakeJob：后台线程跑 make_tape，事件入队（done / error）由主线程消费。
"""

from __future__ import annotations

import queue
from pathlib import Path

from tapemaker.frames import DEFAULT_BIT
from tapemaker.make import make_tape


def default_output(src_text: str) -> str | None:
    """源文件路径 → 默认输出 MP4（同目录同名，扩展名 .mp4）。

    源路径为空或不存在时返回 None（不猜）。
    """
    src = Path(src_text)
    if not src_text or not src.is_file():
        return None
    return str(src.with_suffix(".mp4"))


def validate_config(src_text: str, output_text: str, *, bit_text: str = "",
                    rounds: int = 2, fps: int = 30, pad: int = 3) -> str | None:
    """开始制片前的参数校验，错误返回文案（None = 通过）。

    BIT 留空 = 按分辨率默认档（合法，GUI 提示语已说明）；填了则须为
    1–15 的整数——derive_geometry 的下限校验在 make_tape 里，这里只拦
    明显的输入错误，避免起线程后才失败。
    """
    if not src_text.strip():
        return "请选择待摆渡文件。"
    if not Path(src_text).is_file():
        return f"源文件不存在：{src_text}"
    if not output_text.strip():
        return "请填写输出 MP4 路径。"
    if bit_text.strip():
        try:
            bit = int(bit_text)
        except ValueError:
            return f"BIT 应为 1–15 的整数：{bit_text!r}"
        if not 1 <= bit <= 15:
            return f"BIT 应为 1–15 的整数：{bit}"
    if rounds < 2:
        return "轮次重复至少 2 轮（ADR-0003：轮次重复 N ≥ 2）。"
    if fps < 1:
        return "FPS 至少为 1。"
    if not 3 <= pad <= 15:
        return "PAD（静默区宽，×BIT 像素）应为 3–15。"
    return None


def command_line(src_text: str, output_text: str, *, bit_text: str = "",
                 rounds: int = 2, fps: int = 30, pad: int = 3,
                 resolution: str = "1080p") -> str:
    """GUI 参数 → 等效 CLI 命令文本（命令助手，与 GUI 实际执行一致）。"""
    parts = ["python -m tapemaker make", f'"{src_text}"', "-o", f'"{output_text}"']
    if bit_text.strip():
        parts.append(f"--bit {bit_text.strip()}")
    if pad != 3:  # 默认档不上屏，保持命令最短
        parts.append(f"--pad {pad}")
    parts.append(f"--rounds {rounds}")
    parts.append(f"--fps {fps}")
    if resolution != "1080p":  # 默认档不上屏，保持命令最短
        parts.append(f"--resolution {resolution}")
    return " ".join(parts)


class MakeJob:
    """后台线程跑 make_tape：事件入队（done / error），主线程消费。

    make 无协作式停止点（gzip 与 ffmpeg 写入均为原子段），不提供停止；
    大文件制片耗时以分钟计时，GUI 侧仅显示「制片中…」。
    """

    def __init__(self, src: str, output: str, events: queue.Queue, *,
                 bit_text: str = "", rounds: int = 2, fps: int = 30,
                 pad: int = 3, resolution: str = "1080p"):
        self._src = src
        self._output = output
        self._events = events
        self._kwargs = dict(
            bit=int(bit_text) if bit_text.strip() else None,
            rounds=rounds, fps=fps, pad=pad, resolution=resolution)

    def start(self) -> None:
        import threading
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            summary = make_tape(Path(self._src), Path(self._output), **self._kwargs)
        except Exception as e:  # ValueError / RuntimeError / ffmpeg 缺失等
            self._events.put(("error", str(e)))
            return
        self._events.put(("done", summary))


def summary_text(summary: dict) -> str:
    """make_tape 摘要 → 完成文案（口径与 CLI 打印一致）。"""
    geo = summary["geo"]
    return (
        f"制片完成：{summary['plain_size']} 字节 → gzip {summary['payload_size']} 字节，"
        f"fileId 0x{summary['file_id']:08X}\n"
        f"几何 {geo.canvas_w}×{geo.canvas_h} · BIT {geo.bit} · PAD {geo.pad} · "
        f"{summary['frame_count']} 帧/轮 × {summary['rounds']} 轮 · "
        f"{summary['fps']} fps · 时长 {summary['duration']:.1f}s · "
        f"出片 {summary['out_size']} 字节\n"
        f"已保存到 {summary['output']}\n"
        f"下一步：人工上传到视频平台；对端下载后\n"
        f"python -m receiver receive --source video --video <下载文件> --tape"
    )


def default_bit_hint(resolution: str) -> str:
    """参数区提示文案：BIT 默认档 + 权衡一句话。"""
    return f"BIT 留空 = {resolution} 默认 {DEFAULT_BIT[resolution]} px" \
           f"（越大越抗压缩、片越长；越小片越短、越怕二压）"
