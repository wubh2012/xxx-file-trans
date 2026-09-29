"""制片 GUI 客户端（issue #46）：双击可运行的 tkinter 制片窗口（pythonw
无黑窗口启动）。

tkinter 薄壳：状态、校验、文案、线程编排都在 tapemaker.gui_core（无头
测试），本文件只做控件装配与事件转发。范围只覆盖 make 制片（issue #46
共识），calibrate 定标是专家实验台，仍走 CLI。

导入顺序即 venv 约定：双击 .pyw 时 Windows 用注册的解释器启动，未必是
仓库 venv——依赖都装在 .venv/ 里，先于一切 tapemaker 导入检查并转手
venv pythonw 重启（同 receiver_gui.pyw，issue #26）。

无 DPI awareness 需求（不抓屏、无物理像素坐标），不引入 pick.py。
"""

import queue
import sys
from pathlib import Path

_venv_scripts = Path(__file__).resolve().parent / ".venv" / "Scripts"
_venv_pythonw = _venv_scripts / "pythonw.exe"
if _venv_pythonw.is_file() and Path(sys.executable).resolve().parent != _venv_scripts.resolve():
    import subprocess
    subprocess.Popen([str(_venv_pythonw), str(Path(__file__).resolve())], close_fds=True)
    sys.exit(0)

from tapemaker.gui_core import (MakeJob, command_line, default_bit_hint,
                                default_output, summary_text, validate_config)

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

POLL_MS = 80  # 事件队列轮询周期（GUI 刷新粒度）

RESOLUTION_CHOICES = ["1080p", "4k"]


class TapemakerGui:
    """主窗口装配与事件转发（状态机：待机 ⇄ 制片中，完成回待机）。"""

    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("视频信道制片工具")
        root.minsize(620, 0)
        self.job = None
        self.events: queue.Queue | None = None

        self._build_setup()
        self._build_status()
        self._sync_command()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(POLL_MS, self._poll)

    # ---------- 装配：参数区 ----------

    def _build_setup(self):
        setup = ttk.Frame(self.root, padding=10)
        setup.pack(fill="x")
        self._setup = setup

        self.src_var = tk.StringVar()
        row = ttk.Frame(setup)
        row.pack(fill="x", pady=(0, 6))
        ttk.Label(row, text="待摆渡文件：").pack(side="left")
        ttk.Entry(row, textvariable=self.src_var, width=52).pack(
            side="left", fill="x", expand=True, padx=4)
        ttk.Button(row, text="浏览…", command=self._browse_src).pack(side="left")
        self.src_var.trace_add("write", self._on_src_change)

        self.output_var = tk.StringVar()
        row = ttk.Frame(setup)
        row.pack(fill="x", pady=(0, 6))
        ttk.Label(row, text="输出 MP4：").pack(side="left")
        ttk.Entry(row, textvariable=self.output_var, width=52).pack(
            side="left", fill="x", expand=True, padx=4)
        ttk.Button(row, text="另存为…", command=self._browse_output).pack(side="left")
        self.output_var.trace_add("write", lambda *_: self._sync_command())

        params = ttk.LabelFrame(setup, text="参数", padding=8)
        params.pack(fill="x", pady=(4, 0))
        self.resolution_var = tk.StringVar(value="1080p")
        self.resolution_var.trace_add("write", lambda *_: self._sync_command())
        ttk.Label(params, text="分辨率：").grid(row=0, column=0, sticky="w")
        ttk.Combobox(params, textvariable=self.resolution_var, width=6,
                     values=RESOLUTION_CHOICES, state="readonly").grid(
            row=0, column=1, sticky="w", padx=(0, 12))

        ttk.Label(params, text="BIT：").grid(row=0, column=2, sticky="w")
        self.bit_var = tk.StringVar()
        self.bit_var.trace_add("write", lambda *_: self._sync_command())
        ttk.Entry(params, textvariable=self.bit_var, width=5).grid(
            row=0, column=3, sticky="w", padx=(0, 12))

        ttk.Label(params, text="轮次 N：").grid(row=0, column=4, sticky="w")
        self.rounds_var = tk.IntVar(value=2)
        self.rounds_var.trace_add("write", lambda *_: self._sync_command())
        ttk.Spinbox(params, textvariable=self.rounds_var, from_=2, to=10,
                    width=4).grid(row=0, column=5, sticky="w", padx=(0, 12))

        ttk.Label(params, text="FPS：").grid(row=0, column=6, sticky="w")
        self.fps_var = tk.IntVar(value=30)
        self.fps_var.trace_add("write", lambda *_: self._sync_command())
        ttk.Spinbox(params, textvariable=self.fps_var, from_=1, to=60,
                    width=4).grid(row=0, column=7, sticky="w", padx=(0, 12))

        ttk.Label(params, text="PAD：").grid(row=0, column=8, sticky="w")
        self.pad_var = tk.IntVar(value=3)
        self.pad_var.trace_add("write", lambda *_: self._sync_command())
        ttk.Spinbox(params, textvariable=self.pad_var, from_=3, to=15,
                    width=4).grid(row=0, column=9, sticky="w")

        self.hint_var = tk.StringVar()
        ttk.Label(params, textvariable=self.hint_var, foreground="#555555",
                  wraplength=560, justify="left").grid(
            row=1, column=0, columnspan=10, sticky="w", pady=(4, 0))

        # 命令助手（与 receiver GUI 同风格：等价 CLI 命令 + 复制）
        helper = ttk.LabelFrame(setup, text="命令助手", padding=8)
        helper.pack(fill="x", pady=(8, 0))
        self.command_var = tk.StringVar()
        ttk.Label(helper, textvariable=self.command_var, font=("Consolas", 10),
                  wraplength=560, justify="left", foreground="#1a3d6d").pack(fill="x")
        ttk.Button(helper, text="复制命令", command=self._copy_command).pack(anchor="w", pady=(4, 0))

        btn_row = ttk.Frame(setup)
        btn_row.pack(fill="x", pady=(10, 0))
        self.make_btn = ttk.Button(btn_row, text="开始制片", command=self._start)
        self.make_btn.pack(side="right")
        self._controls = [row, params, self.make_btn]

    # ---------- 装配：状态 / 结果区 ----------

    def _build_status(self):
        self.status_var = tk.StringVar(value="待机：选择待摆渡文件后点「开始制片」")
        self.status_label = ttk.Label(self.root, textvariable=self.status_var,
                                      font=("Microsoft YaHei UI", 10, "bold"),
                                      padding=(10, 6))
        self.status_label.pack(fill="x")

        self.result_var = tk.StringVar()
        self.result_label = ttk.Label(self.root, textvariable=self.result_var,
                                      wraplength=600, justify="left", padding=(10, 4))

    # ---------- 浏览 / 联动 ----------

    def _browse_src(self):
        f = filedialog.askopenfilename(title="选择待摆渡文件")
        if f:
            self.src_var.set(f)

    def _browse_output(self):
        f = filedialog.asksaveasfilename(
            title="选择输出 MP4", defaultextension=".mp4",
            filetypes=[("MP4 视频", "*.mp4"), ("全部文件", "*.*")])
        if f:
            self.output_var.set(f)

    def _on_src_change(self, *_):
        """选了源文件且输出为空时，回填默认输出（同目录同名）。"""
        default = default_output(self.src_var.get())
        if default and not self.output_var.get():
            self.output_var.set(default)
        self._sync_command()

    def _sync_command(self):
        """命令助手实时联动：与 GUI 实际使用的参数一致。

        Spinbox 手输非法值（非整数）时 IntVar.get() 抛 TclError——保号
        显示占位，等 _start 校验拦。
        """
        src, output = self.src_var.get(), self.output_var.get()
        try:
            rounds, fps, pad = (self.rounds_var.get(), self.fps_var.get(),
                                self.pad_var.get())
        except tk.TclError:
            self.command_var.set("（轮次 / FPS / PAD 须为整数）")
            return
        if not src or not output:
            self.command_var.set("（选择待摆渡文件后生成命令）")
            return
        self.hint_var.set(default_bit_hint(self.resolution_var.get()))
        self.command_var.set(command_line(
            src, output, bit_text=self.bit_var.get(), rounds=rounds,
            fps=fps, pad=pad, resolution=self.resolution_var.get()))

    def _copy_command(self):
        text = self.command_var.get()
        if text.startswith("（"):
            return  # 占位提示（参数未填全）不复制
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    # ---------- 开始 / 收尾 ----------

    def _start(self):
        src, output = self.src_var.get().strip(), self.output_var.get().strip()
        try:
            rounds, fps, pad = (self.rounds_var.get(), self.fps_var.get(),
                                self.pad_var.get())
        except tk.TclError:
            messagebox.showerror("参数有误", "轮次 / FPS / PAD 须为整数。",
                                 parent=self.root)
            return
        err = validate_config(src, output, bit_text=self.bit_var.get(),
                              rounds=rounds, fps=fps, pad=pad)
        if err:
            messagebox.showerror("参数有误", err, parent=self.root)
            return
        self.events = queue.Queue()
        self.job = MakeJob(src, output, self.events, bit_text=self.bit_var.get(),
                           rounds=self.rounds_var.get(), fps=self.fps_var.get(),
                           pad=self.pad_var.get(), resolution=self.resolution_var.get())
        self.result_var.set("")
        self.result_label.pack_forget()
        self._set_setup_state("disabled")
        self.status_var.set("制片中…（gzip + 封帧 + ffmpeg 编码，大文件需数分钟）")
        self.job.start()

    def _poll(self):
        """轮询事件队列（主线程消化，tkinter 控件不跨线程触碰）。"""
        if self.events is not None:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                pass
            else:
                self.job = None
                self.events = None
                self._set_setup_state("normal")
                if ev[0] == "done":
                    self.status_var.set("制片完成")
                    self.result_var.set(summary_text(ev[1]))
                    self.result_label.config(foreground="#106b21")
                else:
                    self.status_var.set("制片失败")
                    self.result_var.set(f"制片失败：{ev[1]}")
                    self.result_label.config(foreground="#a12020")
                self.result_label.pack(fill="x")
        self.root.after(POLL_MS, self._poll)

    def _set_setup_state(self, state: str):
        for child in self._controls:
            try:
                child.config(state=state)
            except tk.TclError:
                pass  # 个别容器无 state 的跳过

    def _on_close(self):
        if self.job is not None:
            # make 无协作式停止点；daemon 线程随进程退出，不产生半截文件以外的副作用
            if not messagebox.askyesno(
                    "退出", "制片进行中，确定退出？\n本次制片将中止，输出文件可能不完整。",
                    parent=self.root):
                return
        self.root.destroy()


def main():
    root = tk.Tk()
    TapemakerGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
