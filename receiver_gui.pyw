"""GUI 接收客户端（issue #26）：双击可运行的 tkinter 接收窗口（pythonw
无黑窗口启动；PyInstaller 打包为独立票据）。

tkinter 薄壳：状态、校验、文案、线程编排都在 receiver.gui_core（无头
测试），本文件只做控件装配与事件转发。框选复用 pick.py 冻屏框选
（主窗口隐藏 → 全屏覆盖 → 回填坐标）。一次收一个文件，收完回初始
界面；接收中停止 / 关窗经确认后协作式停止，已收帧保留可续传；
「重新开始」则清空已收进度后不续传重收。

导入顺序即 DPI 约定：先导入 gui_core（连带 receiver.sources.desktop
声明 per-monitor DPI awareness），再创建任何 Tk 控件——--region 物理
像素对齐的前提。
"""

import queue
import time
import os
import sys
from pathlib import Path

# 双击引导（issue #26）：双击 .pyw 时 Windows 用注册的解释器启动，未必是
# 仓库 venv——而 cv2 / rich / mss 等依赖都装在 .venv/ 里，系统解释器在
# 下方 import receiver.* 即失败且 pythonw 无控制台看不到报错。故先于一切
# receiver 导入检查：存在仓库 venv 且当前解释器不在其 Scripts 目录
# （python.exe / pythonw.exe 都算已在 venv 内，测试经 python.exe 直跑不转手）
# 时，转手用 venv pythonw 重启。
_venv_scripts = Path(__file__).resolve().parent / ".venv" / "Scripts"
_venv_pythonw = _venv_scripts / "pythonw.exe"
if _venv_pythonw.is_file() and Path(sys.executable).resolve().parent != _venv_scripts.resolve():
    import subprocess
    subprocess.Popen([str(_venv_pythonw), str(Path(__file__).resolve())], close_fds=True)
    sys.exit(0)

from receiver.gui_core import (GUI_SOURCES, SOURCE_LABELS, ProgressModel,
                               ReceiveJob, build_command, clear_progress,
                               make_frames, progress_tasks, validate_config)
from receiver.notify import default_notifier
from receiver.paths import OUTPUT_DIR
from receiver.pick import pick_region
from receiver.sources.desktop import format_region, parse_region
from receiver.summary import restore_summary

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

POLL_MS = 80  # 事件队列轮询周期（GUI 刷新粒度，含耗时计时器走秒）
CLOSE_TIMEOUT_S = 10  # 关窗确认后等 worker 收尾的宽限，超时强制退出


class ReceiverGui:
    """主窗口装配与事件转发（状态机：待机 ⇄ 接收中，收完回待机）。"""

    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("文件摆渡接收端")
        root.minsize(560, 0)
        self.job: ReceiveJob | None = None
        self.model: ProgressModel | None = None
        self.events: queue.Queue | None = None
        self._closing = False  # 关窗确认后等 done 事件再销毁
        self._close_at = 0.0  # 关窗确认时刻（宽限计时起点）
        self._notifier = default_notifier()

        self._build_setup()
        self._build_progress()
        self._select_source("desktop")  # 初始源 + 命令助手初值
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(POLL_MS, self._poll)

    # ---------- 装配：参数区 ----------

    def _build_setup(self):
        setup = ttk.Frame(self.root, padding=10)
        setup.pack(fill="x")
        self._setup = setup

        self.source_var = tk.StringVar(value="desktop")
        source_row = ttk.Frame(setup)
        source_row.pack(fill="x", pady=(0, 8))
        ttk.Label(source_row, text="取帧源：").pack(side="left")
        for s in GUI_SOURCES:
            ttk.Radiobutton(source_row, text=SOURCE_LABELS[s], value=s,
                            variable=self.source_var,
                            command=self._on_source_change).pack(side="left", padx=(0, 12))

        # 各源参数帧（叠放，按源切换可见性）
        self._param_frames = {}
        self.region_var = tk.StringVar()
        self.dir_var = tk.StringVar()
        self.video_var = tk.StringVar()

        desktop = ttk.Frame(setup)
        ttk.Label(desktop, text="采集区域：").grid(row=0, column=0, sticky="w")
        entry = ttk.Entry(desktop, textvariable=self.region_var, width=26)
        entry.grid(row=0, column=1, padx=4)
        self.region_var.trace_add("write", lambda *_: self._update_command())
        ttk.Button(desktop, text="框选…", command=self._pick_region).grid(row=0, column=2)
        ttk.Label(desktop, text="留空 = 整屏（仅限干净桌面，无白色元素/弹窗遮挡；"
                               "窗口播放建议「框选…」圈住画面）。L,T,W,H 为物理像素").grid(
            row=1, column=1, columnspan=2, sticky="w", pady=(2, 0))
        desktop.columnconfigure(1, weight=1)
        self._param_frames["desktop"] = desktop

        images = ttk.Frame(setup)
        ttk.Label(images, text="PNG 目录：").grid(row=0, column=0, sticky="w")
        ttk.Entry(images, textvariable=self.dir_var, width=44).grid(row=0, column=1, padx=4)
        ttk.Button(images, text="浏览…",
                   command=self._browse_dir).grid(row=0, column=2)
        self.dir_var.trace_add("write", lambda *_: self._update_command())
        images.columnconfigure(1, weight=1)
        self._param_frames["images"] = images

        video = ttk.Frame(setup)
        ttk.Label(video, text="视频文件：").grid(row=0, column=0, sticky="w")
        ttk.Entry(video, textvariable=self.video_var, width=44).grid(row=0, column=1, padx=4)
        ttk.Button(video, text="浏览…",
                   command=self._browse_video).grid(row=0, column=2)
        self.video_var.trace_add("write", lambda *_: self._update_command())
        video.columnconfigure(1, weight=1)
        self._param_frames["video"] = video

        # 命令助手（desktop 联动为第一版重点，images / video 同样生成）
        helper = ttk.LabelFrame(setup, text="发送端命令助手", padding=8)
        self._helper = helper  # 源参数帧 pack 以它为锚（保持参数区在助手上方）
        helper.pack(fill="x", pady=(8, 0))
        self.guidance_var = tk.StringVar()
        ttk.Label(helper, textvariable=self.guidance_var, foreground="#555555",
                  wraplength=520, justify="left").pack(fill="x")
        self.command_var = tk.StringVar()
        command_label = ttk.Label(helper, textvariable=self.command_var,
                                  font=("Consolas", 10), wraplength=520,
                                  justify="left", foreground="#1a3d6d")
        command_label.pack(fill="x", pady=(4, 4))
        ttk.Button(helper, text="复制命令", command=self._copy_command).pack(anchor="w")

        # 重新开始 = 清空已收进度后重收（不续传，区别于停止后重收的续传语义）
        btn_row = ttk.Frame(setup)
        btn_row.pack(fill="x", pady=(10, 0))
        self.restart_btn = ttk.Button(btn_row, text="重新开始",
                                      command=self._restart)
        self.restart_btn.pack(side="right")
        self.start_btn = ttk.Button(btn_row, text="开始接收",
                                    command=self._start)
        self.start_btn.pack(side="right", padx=(0, 8))

        # 接收中置灰的控件面（命令助手除外：发送端此时正需要照抄命令）
        self._controls = [source_row, *self._param_frames.values(),
                          self.start_btn, self.restart_btn]

    # ---------- 装配：进度 / 结果区 ----------

    def _build_progress(self):
        progress = ttk.LabelFrame(self.root, text="接收进度", padding=10)
        self._progress = progress  # 接收中才显示
        row = ttk.Frame(progress)
        row.pack(fill="x")
        self.status_var = tk.StringVar(value="待机")
        ttk.Label(row, textvariable=self.status_var,
                  font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        self.stop_btn = ttk.Button(row, text="停止", command=self._stop,
                                   state="disabled")
        self.stop_btn.pack(side="right")
        progress.pack(fill="x", padx=10, pady=(0, 4))
        progress.pack_forget()

        self.result_var = tk.StringVar()
        self.result_label = ttk.Label(self.root, textvariable=self.result_var,
                                      wraplength=540, justify="left", padding=(10, 4))

    # ---------- 源切换 / 参数联动 ----------

    def _on_source_change(self):
        self._select_source(self.source_var.get())

    def _select_source(self, source: str):
        self.source_var.set(source)
        for name, frame in self._param_frames.items():
            if name == source:
                # before=_helper：pack 追加会掉到命令助手下方，以它为锚保持顺序
                frame.pack(fill="x", pady=2, before=self._helper)
            else:
                frame.pack_forget()
        self.guidance_var.set({
            "desktop": "先在发送端开始播放（窗口播放模式），再点「开始接收」；"
                       "框选只需完整包含帧画布并留边，无需像素级精确。",
            "images": "选择发送端导出的 PNG 帧序列目录（frames_png 布局）。",
            "video": "选择发送端画面的预录屏幕视频文件。",
        }[source])
        self._update_command()

    def _update_command(self):
        """命令助手实时联动：与 GUI 实际使用的参数一致（含具体坐标）。"""
        source = self.source_var.get()
        if source == "images" and not self.dir_var.get():
            self.command_var.set("（选择 PNG 目录后生成命令）")
            return
        if source == "video" and not self.video_var.get():
            self.command_var.set("（选择视频文件后生成命令）")
            return
        try:
            region = self._region_or_none()
        except ValueError as e:
            self.command_var.set(f"（区域格式应为 L,T,W,H：{e}）")
            return
        self.command_var.set(build_command(
            source, frames_dir=self.dir_var.get() or None,
            video=self.video_var.get() or None, region=region))

    def _region_or_none(self) -> dict | None:
        """区域输入 → mss region dict；留空 = 整屏；格式非法抛 ValueError。"""
        text = self.region_var.get().strip()
        return parse_region(text) if text else None

    # ---------- 框选 / 浏览 / 复制 ----------

    def _pick_region(self):
        """复用 pick.py 冻屏框选：主窗口隐藏 → 全屏覆盖 → 回填坐标（物理像素）。"""
        self.root.withdraw()
        try:
            region = pick_region()
        except tk.TclError as e:
            # pythonw 无控制台，Tk 回调异常默认无声吞掉——显式弹出，
            # 框选故障（如 Tk 解释器/显示问题）不静默
            messagebox.showerror("框选失败", str(e), parent=self.root)
            region = None
        finally:
            self.root.deiconify()
        if region is not None:  # 取消（Esc）不动已填值
            self.region_var.set(format_region(region))

    def _browse_dir(self):
        d = filedialog.askdirectory(title="选择 PNG 帧序列目录")
        if d:
            self.dir_var.set(d)

    def _browse_video(self):
        f = filedialog.askopenfilename(title="选择录制视频文件")
        if f:
            self.video_var.set(f)

    def _copy_command(self):
        text = self.command_var.get()
        if text.startswith("（"):
            return  # 占位提示（参数未填全）不复制
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    # ---------- 开始 / 重新开始 / 停止 / 收尾 ----------

    def _restart(self):
        """重新开始（issue #26）：清空 progress/ 全部已收进度后按当前参数
        重收——同 fileId 也不续传，与「停止后重收」的续传语义相对。"""
        tasks = progress_tasks()
        detail = (f"将删除 {len(tasks)} 个未完成任务目录的已收帧，"
                  "重新接收同一文件不续传。" if tasks else
                  "当前没有已收进度，效果与「开始接收」相同。")
        if not messagebox.askyesno("重新开始",
                                   f"确定重新开始？\n{detail}",
                                   parent=self.root):
            return
        try:
            clear_progress()
        except OSError as e:  # 个别目录删不掉（句柄占用等），不带着旧进度开收
            messagebox.showerror("清除进度失败", str(e), parent=self.root)
            return
        self._start()

    def _start(self):
        source = self.source_var.get()
        region_text = self.region_var.get().strip()
        dir_text = self.dir_var.get() or None
        video_text = self.video_var.get() or None
        err = validate_config(source, frames_dir=dir_text, video=video_text,
                              region_text=region_text or None)
        if err:
            messagebox.showerror("参数有误", err, parent=self.root)
            return
        try:
            region = parse_region(region_text) if region_text else None
        except ValueError as e:  # validate_config 已拦，防御性兜底
            messagebox.showerror("参数有误", str(e), parent=self.root)
            return

        source_params = {"frames_dir": dir_text, "video": video_text, "region": region}
        self.model = ProgressModel()
        self.events = queue.Queue()
        self.job = ReceiveJob(lambda: make_frames(source, **source_params),
                              OUTPUT_DIR, self.events, notify=self._notifier,
                              prefetch=(source == "desktop"))
        self.result_var.set("")
        self.result_label.pack_forget()
        self._set_setup_state("disabled")
        self._progress.pack(fill="x", padx=10, pady=(0, 4))
        self.stop_btn.config(state="normal")
        self.status_var.set("接收中…")
        self.job.start()

    def _stop(self):
        if self.job is None:
            return
        if not messagebox.askyesno(
                "停止接收", "确定停止接收？\n已收帧将保留，重新接收同一文件（同 fileId）可续传。",
                parent=self.root):
            return
        self.job.stop()
        self.stop_btn.config(state="disabled")
        self.status_var.set("正在停止…")

    def _poll(self):
        """轮询事件队列（主线程消化，tkinter 控件不跨线程触碰）。"""
        if self._closing and time.monotonic() - self._close_at > CLOSE_TIMEOUT_S:
            self.root.destroy()  # worker 挂死兜底：宽限期到强制退出
            return
        if self.events is not None:
            try:
                while True:
                    ev = self.events.get_nowait()
                    if ev[0] == "done":
                        self._on_done(ev[1])
                        return  # _on_done 决定重启轮询或退出
                    self.model.on_event(ev)
            except queue.Empty:
                pass
            self.status_var.set(self.model.summary_line())
        self.root.after(POLL_MS, self._poll)

    def _on_done(self, result):
        self.job = None
        self.events = None
        self._set_setup_state("normal")
        self.stop_btn.config(state="disabled")
        self._progress.pack_forget()
        self.status_var.set("待机")
        # 非致命告警（meta 落盘降级等，issue #43）随结果一并露出，不推翻成败
        suffix = "".join(f"\n{w}" for w in result.warnings)
        if result.code == 0:
            # 与 #25 摘要口径一致：友好大小 / 耗时 / 速率 / 帧 N/M / sha256
            self.result_var.set(
                restore_summary(result.name, result.plain_size, result.elapsed,
                                result.received, result.total, result.sha256)
                + f"\n已保存到 {result.dest}" + suffix)
            self.result_label.config(foreground="#106b21")
        elif result.stopped:
            self.result_var.set(
                f"已停止：{result.incomplete}\n"
                f"已保留 {result.received} 帧，重新接收同一文件（同 fileId）可续传。"
                + suffix)
            self.result_label.config(foreground="#8a6d00")
        else:
            self.result_var.set(
                f"接收失败：{result.error}"
                + (f"\n{result.incomplete}" if result.incomplete else "")
                + suffix)
            self.result_label.config(foreground="#a12020")
        self.result_label.pack(fill="x")
        if self._closing:
            self.root.destroy()
            return
        self.root.after(POLL_MS, self._poll)  # 回初始界面，可再次开始

    def _set_setup_state(self, state: str):
        for child in self._controls:
            try:
                child.config(state=state)
            except tk.TclError:
                pass  # Label 等无 state 的控件跳过

    def _on_close(self):
        if self.job is not None:
            if not messagebox.askyesno(
                    "退出", "接收进行中，确定退出？\n已收帧将保留，重新接收同一文件（同 fileId）可续传。",
                    parent=self.root):
                return
            self.job.stop()
            self._closing = True  # done 事件到达后销毁（worker 落盘收尾不被打断）
            self._close_at = time.monotonic()
            return
        self.root.destroy()


def main():
    root = tk.Tk()
    ReceiverGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
