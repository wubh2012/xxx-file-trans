"""GUI 接收客户端的无头逻辑层（issue #26）：tkinter 薄壳（receiver_gui.pyw）
只做控件装配与事件转发，状态、校验、文案、线程编排全部在此模块，
可无头测试（同 pick.py「纯函数拆出 + 薄壳人工验收」惯例）。

架构（grilling 共识 2026-09-23）：同进程后台线程直接复用 receiver 内部
接收核心（receiver.run.run_receive），进度经 typed 元组事件入队、由 GUI
主线程 root.after 轮询消费——不解析任何文本输出。stop() 为协作式停止：
run_receive 逐帧检查，已收帧照常持久化（FrameStore 断点续传）。

DPI 感知：本模块导入 receiver.sources.desktop 即声明 per-monitor DPI
awareness（模块导入副作用），薄壳须先导入本模块再创建任何 Tk 控件，
--region 物理像素对齐的前提。
"""

import json
import queue
import shutil
import threading
import time
from pathlib import Path

from receiver import paths
from receiver.pipeline import FrameRejected
from receiver.run import ReceiveResult, run_receive
from receiver.sources import iter_source
from receiver.sources.desktop import format_region, iter_desktop, parse_region
from receiver.sources.video import iter_video
from receiver.summary import format_elapsed

COMMAND_PREFIX = "python -m receiver receive"

# GUI 源选项（CLI 的 camera 为骨架，GUI 第一版不提供）
GUI_SOURCES = ("desktop", "images", "video")

SOURCE_LABELS = {
    "desktop": "抓屏（desktop）",
    "images": "PNG 序列（images）",
    "video": "视频文件（video）",
}


def build_command(source: str, *, frames_dir=None, video=None, region=None,
                  tape=False) -> str:
    """GUI 当前参数 → 等效完整命令文本（发送端命令助手，issue #26）。

    与 CLI 实际使用的参数一致（含具体坐标）：desktop region 为 None
    （整屏）时省略 --region，与 CLI 缺省语义一致；路径参数加双引号，
    含空格路径可直接粘贴到 PowerShell / 终端。
    """
    if source == "desktop":
        # 与 CLI 当前默认一致：Windows 优先 DXGI，不可用时回退 mss。
        cmd = f"{COMMAND_PREFIX} --source desktop --capture auto"
        if region is not None:
            cmd += f" --region {format_region(region)}"
        return cmd
    if source == "images":
        return f'{COMMAND_PREFIX} --source images --dir "{frames_dir}"'
    if source == "video":
        cmd = f'{COMMAND_PREFIX} --source video --video "{video}"'
        if tape:
            cmd += " --tape"   # 片模式（issue #45）：制片 MP4 旁路稳定闸门
        return cmd
    raise ValueError(f"未知源 {source}")


def validate_config(source: str, *, frames_dir=None, video=None,
                    region_text=None) -> str | None:
    """开始接收前的参数校验：返回错误文案，None = 通过。

    desktop region 留空 = 整屏（monitors[0]），格式非法 / 宽高非正的
    文案来自 parse_region（与 CLI 用法错误同源）。
    """
    if source == "desktop":
        if region_text:
            try:
                parse_region(region_text)
            except ValueError as e:
                return str(e)
        return None
    if source == "images":
        if not frames_dir:
            return "请选择 PNG 帧序列目录"
        if not Path(frames_dir).is_dir():
            return f"PNG 帧序列目录不存在：{frames_dir}"
        return None
    if source == "video":
        if not video:
            return "请选择录制视频文件"
        if not Path(video).is_file():
            return f"视频文件不存在：{video}"
        return None
    return f"未知源 {source}"


def make_frames(source: str, *, frames_dir=None, video=None, region=None,
                tape=False, on_backend=None):
    """GUI 参数 → 取帧源迭代器（与 CLI 同分派，issue #26）。

    desktop / video 生成器惰性打开：真实抓屏 / 解码在接收线程首次迭代
    时发生（mss 实例归属创建线程，天然落在 worker 线程内）。
    """
    if source == "images":
        return iter_source("images", Path(frames_dir))
    if source == "desktop":
        return iter_desktop(region=region, on_backend=on_backend)
    if source == "video":
        return iter_video(video, tape=tape)
    raise ValueError(f"未知源 {source}")


def progress_tasks(progress_dir: Path | None = None) -> list[Path]:
    """progress/ 下未完成任务目录列表（重新开始确认文案用，issue #26）。

    progress_dir None = 锚定 paths.PROGRESS_DIR（运行时取属性，测试
    monkeypatch 生效）；目录不存在视为无任务。
    """
    root = paths.PROGRESS_DIR if progress_dir is None else Path(progress_dir)
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir())


def clear_progress(progress_dir: Path | None = None) -> list[str]:
    """清空全部未完成任务目录（GUI「重新开始」，issue #26）：已收帧作废，
    同 fileId 重收不续传。返回已删除的任务目录名；无任务目录为空操作。
    个别目录删除失败（如句柄占用）时删完其余后抛 OSError，不静默吞错。
    """
    removed, errors = [], []
    for task in progress_tasks(progress_dir):
        try:
            shutil.rmtree(task)
            removed.append(task.name)
        except OSError as e:
            errors.append(f"{task.name}：{e}")
    if errors:
        raise OSError("；".join(errors))
    return removed


class ProgressModel:
    """GUI 进度模型：typed 帧事件 → 汇总状态 + 单行文案。

    口径与 CLI ProgressReporter 一致：识别率 = 识别成功画面 / 读入画面
    （重复帧计入分子分母，丢弃帧计入分母）；耗时锚点 = 首个 is_new
    数据帧（等待发送端不计入，#25）。clock 可注入（无头测试）。
    """

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.total: int | None = None
        self.received = 0
        self.seen = 0  # 读入画面总数（识别率分母）
        self.discarded = 0  # 丢弃画面数（识别率减项）
        self._t0: float | None = None
        self.capture_ready = False

    def on_event(self, ev: tuple) -> None:
        """消化一条 QueuedReporter 事件（decoded / rejected / total）。"""
        kind = ev[0]
        if kind == "capture_ready":
            self.capture_ready = True
        elif kind == "decoded":
            is_new = ev[1]
            self.seen += 1
            if is_new:
                self.received += 1
                if self._t0 is None:
                    self._t0 = self.clock()
        elif kind == "rejected":
            self.seen += 1
            self.discarded += 1
        elif kind == "total":
            self.total, completed = ev[1], ev[2]
            self.received = completed  # 断点续传重同步（同 set_total 语义）

    @property
    def elapsed(self) -> float:
        return 0.0 if self._t0 is None else self.clock() - self._t0

    @property
    def rate(self) -> float:
        if not self.seen:
            return 100.0
        return 100.0 * (self.seen - self.discarded) / self.seen

    def summary_line(self) -> str:
        """进度单行：「已收 N/M 帧（P%） · 识别率 R% · 耗时 T s」。"""
        if self.capture_ready and self.total is None:
            return "采集已就绪，请在发送端开始播放；保持画面无遮挡"
        total_s = "?" if self.total is None else self.total
        line = f"已收 {self.received}/{total_s} 帧"
        if self.total is not None:
            line += f"（{100.0 * self.received / self.total:.0f}%）"
        line += f" · 识别率 {self.rate:.1f}% · 耗时 {format_elapsed(self.elapsed)}"
        return line


class QueuedReporter:
    """ProgressReporter 同接口的 GUI 替身（issue #26 注入缝）。

    帧事件转为 typed 元组推入线程安全队列：
      ("decoded", is_new, payload_len) / ("rejected", reason, detail) /
    ("total", total, completed) / ("finish",)；GUI 主线程 root.after
    轮询消化（tkinter 控件只在主线程触碰）。
    """

    def __init__(self, events: queue.Queue):
        self._events = events

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def set_total(self, total: int, completed: int | None = None) -> None:
        self._events.put(("total", total, completed))

    def on_decoded(self, payload: bytes, is_new: bool) -> None:
        self._events.put(("decoded", is_new, len(payload)))

    def on_rejected(self, name: str, e: FrameRejected) -> None:
        self._events.put(("rejected", e.reason, e.detail))

    def finish(self) -> None:
        self._events.put(("finish",))


class JsonlTraceWriter:
    """线程安全的 UTF-8 JSONL 接收诊断日志写入器。"""

    _FLUSH_EVENTS = {
        "gui_session_start", "capture_backend", "progress_milestone",
        "progress_stall", "stream_idle", "restore_stage_start",
        "restore_stage", "restore_error", "receive_incomplete",
        "receive_done", "gui_job_done",
    }

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("x", encoding="utf-8", newline="\n")
        self._lock = threading.Lock()
        self._closed = False
        self._pending = 0
        self._last_flush = time.monotonic()

    def __call__(self, event: dict) -> None:
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            if self._closed:
                return
            self._stream.write(line + "\n")
            self._pending += 1
            now = time.monotonic()
            if (event.get("event") in self._FLUSH_EVENTS or
                    self._pending >= 25 or now - self._last_flush >= 1.0):
                self._stream.flush()
                self._pending = 0
                self._last_flush = now

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                try:
                    self._stream.flush()
                finally:
                    self._stream.close()


class ReceiveJob:
    """一次接收任务：后台线程跑 run_receive，typed 事件与结果经队列上浮。

    stop() 置协作式停止标志（run_receive 逐帧检查，已收帧照常持久化，
    续传语义与 Ctrl+C 一致）；worker 线程异常兜底为错误结果事件，不击穿
    GUI；done 事件必达（finally 兜底），GUI 据此收尾。
    """

    def __init__(self, frames_factory, out_dir: Path, events: queue.Queue,
                 notify=None, prefetch: bool = False, trace=None):
        self._frames_factory = frames_factory
        self._out_dir = Path(out_dir)
        self._events = events
        self._notify = notify
        self._prefetch = prefetch
        self._trace = trace
        self._stop_flag = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True,
                         name="receive-job").start()

    def stop(self) -> None:
        self._stop_flag.set()

    def _run(self) -> None:
        frames = None
        try:
            frames = self._frames_factory()
            result = run_receive(
                frames, self._out_dir,
                reporter_factory=lambda: QueuedReporter(self._events),
                notify=self._notify,
                stop_check=self._stop_flag.is_set,
                prefetch=self._prefetch,
                trace=self._trace,
            )
        except Exception as e:  # 兜底：取帧源打不开等异常转错误结果
            result = ReceiveResult(code=1, error=f"接收异常：{e}")
        finally:
            if frames is not None and hasattr(frames, "close"):
                try:
                    frames.close()  # 协作停止后生成器不再被消费，显式释放采集资源
                except ValueError:
                    # C3 生产者线程可能正在 generator.next()；停止标志已置位，
                    # 跨线程 close 失败时由生产者自行在下一次取帧后退出。
                    pass
            if self._trace is not None:
                try:
                    self._trace({"event": "gui_job_done", "code": result.code,
                                 "stopped": result.stopped,
                                 "error": result.error})
                except Exception:  # noqa: BLE001 诊断日志故障不得改变接收结果
                    pass
                close_trace = getattr(self._trace, "close", None)
                if close_trace is not None:
                    try:
                        close_trace()
                    except Exception:  # noqa: BLE001 诊断日志故障不得改变接收结果
                        pass
            self._events.put(("done", result))
