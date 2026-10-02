"""headless 接收核心（issue #26）：接收主循环与 CLI 文本输出解耦。

CLI（receiver.cli）与 tkinter GUI（receiver_gui.pyw）共用同一套 FrameStore /
解码 / 还原逻辑：进度经 reporter_factory 注入（CLI 用 rich ProgressReporter，
GUI 用 typed 事件队列 QueuedReporter），notify 为完成通知边界（issue #12），
stop_check 为协作式停止缝——逐帧检查，置真即停，已收帧照常持久化
（断点续传语义与 Ctrl+C 一致，issue #26）；停下的那一刻已收齐则照常还原。

本模块不打印、不依赖 tty：全部用户可见信息经结构化 ReceiveResult 交还
前端渲染。CLI 输出契约（issue #25 摘要口径）由 cli.receive_frames 薄壳
按结果字段渲染，行为不变。
"""

import hashlib
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from receiver import paths
from receiver.pipeline import DecodedFrame, FrameRejected, GeometryCache, decode_frame
from receiver.progress import ProgressReporter
from receiver.restore import RestoreError, gunzip_verify
from receiver.sanitize import safe_dest, sanitize_filename
from receiver.store import FrameStore, IncompleteError
from receiver.summary import RestoreTimer, friendly_size, incomplete_summary


@dataclass
class ReceiveResult:
    """一次接收任务的结构化结果（前端据此渲染各自的完成 / 失败画面）。"""

    code: int  # 0 = 还原成功；1 = 未收齐 / 失败 / 停止（与 CLI 退出码同口径）
    stopped: bool = False  # stop_check 协作式停止（GUI 停止按钮 / 关窗）
    dest: Path | None = None  # 还原落盘路径（code 0）
    name: str = ""  # 净化后的落盘文件名
    plain_size: int = 0
    elapsed: float = 0.0  # 耗时口径到写盘完成止（与 #25 restore_summary 一致）
    received: int = 0
    total: int | None = None
    sha256: str = ""
    error: str | None = None  # 失败原因（code 1 且非停止）
    incomplete: str | None = None  # 未完成统计行（#25 口径，失败 / 停止路径）
    warnings: list[str] = field(default_factory=list)  # 非致命告警（清理 / 通知失败）
    queue_produced: int = 0  # C3：生产者产出的候选帧数
    queue_dropped: int = 0  # C3：有界队列为保留最新帧而丢弃的候选帧数


@dataclass
class FrameQueueStats:
    """C3 预取层的可观测统计，不参与协议或还原判据。"""

    produced: int = 0
    enqueued: int = 0
    dropped: int = 0


class PrefetchedFrames:
    """将取帧源与解码消费者解耦的有界“最新帧”队列。

    该层只用于实时 desktop 源：消费者跟不上时丢弃队列中较旧的候选，
    让解码器尽快看到最新画面。images/video 等有限源默认不启用，避免
    为了吞吐而改变离线帧序列的完整性语义。
    """

    _END = object()

    def __init__(self, frames, *, maxsize: int = 2, on_idle=None, stop_check=None):
        if maxsize < 1:
            raise ValueError("maxsize 必须 ≥ 1")
        self._frames = frames
        self._on_idle = on_idle
        self._stop_check = stop_check
        self._queue: queue.Queue = queue.Queue(maxsize=maxsize)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.stats = FrameQueueStats()
        self.error: BaseException | None = None
        self.cleanup_error: Exception | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._produce, daemon=True, name="frame-producer"
        )
        self._thread.start()

    def _produce(self) -> None:
        try:
            for item in self._frames:
                if self._stop.is_set():
                    break
                self.stats.produced += 1
                try:
                    self._queue.put_nowait(item)
                except queue.Full:
                    # 实时画面只保留最新候选；解码器不会继续处理已经
                    # 过时的屏幕画面。丢弃量单独统计，不能与 CRC 拒帧混淆。
                    try:
                        self._queue.get_nowait()
                    except queue.Empty:
                        pass
                    else:
                        self.stats.dropped += 1
                    try:
                        self._queue.put_nowait(item)
                    except queue.Full:
                        # 消费者刚好取走/竞争时，宁可记录一次丢弃也不阻塞
                        # 生产线程；下一轮会继续尝试。
                        self.stats.dropped += 1
                        continue
                self.stats.enqueued += 1
        except BaseException as exc:  # 在消费者线程重新抛出，保留原异常
            self.error = exc
        finally:
            # 原始采集器由生产线程创建和释放；MSS 的 Windows DC 不能跨线程释放。
            close = getattr(self._frames, "close", None)
            if close is not None:
                try:
                    close()
                except Exception as exc:
                    self.cleanup_error = exc
            if not self._stop.is_set():
                while True:
                    try:
                        self._queue.put(self._END, timeout=0.05)
                        break
                    except queue.Full:
                        if self._stop.is_set():
                            break

    def __iter__(self):
        self.start()
        return self

    def __next__(self):
        idle_at = time.monotonic()
        while True:
            if self._stop.is_set() or (self._stop_check is not None and self._stop_check()):
                raise StopIteration
            try:
                item = self._queue.get(timeout=0.1)
                break
            except queue.Empty:
                if self._on_idle is not None and time.monotonic() - idle_at >= 1.0:
                    self._on_idle(self.stats, self._queue.qsize())
                    idle_at = time.monotonic()
        if item is self._END:
            if self.error is not None:
                raise self.error
            raise StopIteration
        return item

    def close(self) -> None:
        self._stop.set()
        request_stop = getattr(self._frames, "request_stop", None)
        if request_stop is not None:
            request_stop()
        if self._thread is None:
            # 尚未启动的惰性源没有线程资源，可在调用方关闭。
            close = getattr(self._frames, "close", None)
            if close is not None:
                close()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=0.5)


def run_receive(frames, out_dir: Path, reporter_factory=ProgressReporter,
                notify=None, stop_check=None, *, prefetch: bool = False,
                prefetch_size: int = 2, trace=None, allow_scaled=False) -> ReceiveResult:
    """接收主循环：frames 为任一取帧源的 (名称, 灰度图) 迭代器。

    reporter_factory 返回 ProgressReporter 同接口对象（上下文管理器 +
    on_decoded / on_rejected / set_total / finish，issue #26 注入缝）；
    stop_check 非 None 时逐帧调用，返回真即协作式停止。
    prefetch 为真时启用 C3 有界生产者/消费者队列，适用于实时 desktop
    源；prefetch_size 为队列容量，默认只保留少量最新候选帧。
    trace 非 None 时收到结构化诊断事件；默认关闭，不影响常规接收。
    allow_scaled 由 desktop/video 入口启用，容忍播放器显示缩放；
    原始 images 输入仍按像素几何严格校验。
    """
    run_started = time.perf_counter()
    trace_failed = False

    def emit(event: str, **fields) -> None:
        nonlocal trace_failed
        if trace is None or trace_failed:
            return
        try:
            trace({"event": event,
                   "t_rel_s": round(time.perf_counter() - run_started, 6),
                   **fields})
        except Exception:
            # Diagnostics must not turn a successful receive into a failure.
            trace_failed = True

    out_dir = Path(out_dir)
    def on_stream_idle(stats, queue_depth):
        emit("stream_idle", produced=stats.produced, enqueued=stats.enqueued,
             dropped=stats.dropped, queue_depth=queue_depth)

    prefetched = (PrefetchedFrames(frames, maxsize=prefetch_size,
                                   on_idle=on_stream_idle if trace is not None else None,
                                   stop_check=stop_check)
                  if prefetch else None)
    stream = prefetched if prefetched is not None else frames
    if prefetched is not None:
        prefetched.start()
    # 断点续传（issue #7）：任务目录锚定 progress/，首个数据帧落地即锁定，
    # 崩溃 / Ctrl+C 重启后惰性加载已收帧，只补缺失帧
    store = FrameStore(paths.PROGRESS_DIR)
    timer = RestoreTimer()  # 还原计时（issue #25）：锚点 = 首个 is_new 数据帧落地
    emit("receive_start", prefetch=prefetch, prefetch_size=prefetch_size,
         allow_scaled=allow_scaled)

    def fail(msg: str | None, *, stopped: bool = False) -> ReceiveResult:
        """统一未完成出口：失败原因 + 未完成统计（issue #25），退出码 1。"""
        emit("receive_incomplete", stopped=stopped, error=msg,
             received=store.received_count(), total=store.total_frames,
             missing_frame_numbers=store.missing_frame_numbers(limit=32))
        result = ReceiveResult(
            code=1, stopped=stopped, error=msg,
            received=store.received_count(), total=store.total_frames,
            incomplete=incomplete_summary(timer.elapsed,
                                          store.received_count(), store.total_frames),
        )
        result.warnings.extend(store.warnings)
        if prefetched is not None:
            result.queue_produced = prefetched.stats.produced
            result.queue_dropped = prefetched.stats.dropped
        return result

    stopped = False
    geo_cache = GeometryCache(allow_scaled=allow_scaled)  # 显示缩放仅由采集源显式启用
    last_candidate_at = None
    last_progress_pulse = run_started
    last_progress_label = None
    try:
        with reporter_factory() as reporter:
            try:
                for name, img in stream:
                    candidate_at = time.perf_counter()
                    gap_ms = (None if last_candidate_at is None else
                              round((candidate_at - last_candidate_at) * 1000, 3))
                    last_candidate_at = candidate_at
                    if stop_check is not None and stop_check():
                        stopped = True
                        break
                    stage_started = time.perf_counter()
                    try:
                        frame: DecodedFrame = decode_frame(img, geo_cache)
                    except FrameRejected as e:
                        save_frame = getattr(trace, "save_rejected_frame", None)
                        if save_frame is not None:
                            try:
                                save_frame(img, e.reason)
                            except Exception:
                                pass  # 诊断留样不得中断接收
                        emit("frame_rejected", stage="decode", reason=e.reason,
                             detail=e.detail, gap_ms=gap_ms,
                             duration_ms=round((time.perf_counter() - stage_started) * 1000, 3),
                             received=store.received_count(), total=store.total_frames)
                        reporter.on_rejected(name, e)
                        continue
                    decode_ms = round((time.perf_counter() - stage_started) * 1000, 3)
                    stage_started = time.perf_counter()
                    try:
                        is_new = store.add(frame)
                    except FrameRejected as e:
                        # 跨任务混帧 / 参数锁定硬锁（param_lock）同样整帧拒绝
                        emit("frame_rejected", stage="store", frame_no=frame.header.frame_no,
                             reason=e.reason, detail=e.detail, gap_ms=gap_ms,
                             decode_ms=decode_ms,
                             duration_ms=round((time.perf_counter() - stage_started) * 1000, 3),
                             received=store.received_count(), total=store.total_frames)
                        reporter.on_rejected(name, e)
                        continue
                    store_ms = round((time.perf_counter() - stage_started) * 1000, 3)
                    if is_new:
                        timer.start()  # 与参数锁定同点起算，首帧前时间不计入
                    reporter.on_decoded(frame.payload, is_new)
                    if store.total_frames is not None:
                        reporter.set_total(store.total_frames,
                                           completed=store.received_count())
                    received = store.received_count()
                    total = store.total_frames
                    percent = None if total is None else 100 * received / total
                    progress_label = (None if percent is None else
                                      "100%" if received == total else
                                      f"{int(percent)}%")
                    frame_kind = ("metadata" if frame.header.frame_no == 0xFFFFFF else
                                  "fec" if frame.header.frame_no >= frame.header.total_frames else
                                  "data")
                    emit("frame", frame_no=frame.header.frame_no,
                         frame_kind=frame_kind, is_new=is_new,
                         received=received, total=total, percent=percent,
                         gap_ms=gap_ms, decode_ms=decode_ms, store_ms=store_ms)
                    if (is_new and progress_label in {"90%", "95%", "99%", "100%"}
                            and progress_label != last_progress_label):
                        emit("progress_milestone", label=progress_label,
                             received=received, total=total,
                             missing_count=total - received,
                             missing_frame_numbers=store.missing_frame_numbers(limit=32))
                        last_progress_label = progress_label
                        last_progress_pulse = time.perf_counter()
                    elif (not is_new and percent is not None and percent >= 98.5
                          and time.perf_counter() - last_progress_pulse >= 5.0):
                        emit("progress_stall", received=received, total=total,
                             percent=percent, missing_count=total - received,
                             missing_frame_numbers=store.missing_frame_numbers(limit=32))
                        last_progress_pulse = time.perf_counter()
                    if store.is_complete():
                        # 收齐判据满足即提前退出进入还原（F13/验收标准 2，
                        # issue #19）：desktop 等无限源不等流耗尽。元数据
                        # 缺失时永不 complete，照常继续收帧。
                        break
            except KeyboardInterrupt:
                return fail("接收中断（Ctrl+C）：进度已持久化，重新运行将只补缺失帧")
            stopped = stopped or (stop_check is not None and stop_check())
            reporter.finish()
    finally:
        if prefetched is not None:
            prefetched.close()
        store.close()

    if stopped and not store.is_complete():
        # 协作式停止（issue #26）：停止不是失败（error 置空），已收帧保留可续传
        return fail(None, stopped=True)

    if store.is_complete():
        try:
            emit("restore_stage_start", stage="assemble")
            stage_started = time.perf_counter()
            assembled = store.assemble()
            emit("restore_stage", stage="assemble", duration_ms=round(
                (time.perf_counter() - stage_started) * 1000, 3),
                compressed_bytes=len(assembled))
            emit("restore_stage_start", stage="gunzip_verify",
                 compressed_bytes=len(assembled))
            stage_started = time.perf_counter()
            plain = gunzip_verify(assembled)
            emit("restore_stage", stage="gunzip_verify", duration_ms=round(
                (time.perf_counter() - stage_started) * 1000, 3),
                plain_bytes=len(plain))
        except (IncompleteError, RestoreError) as e:
            emit("restore_error", stage="assemble_or_gunzip", error=str(e))
            return fail(f"还原失败：{e}")
        if len(plain) != store.metadata.plain_size:
            return fail(
                f"还原失败：plainSize 不一致（元数据声明 {store.metadata.plain_size}，"
                f"实际解压 {len(plain)}）"
            )
        emit("restore_stage_start", stage="mkdir_output")
        stage_started = time.perf_counter()
        out_dir.mkdir(parents=True, exist_ok=True)
        emit("restore_stage", stage="mkdir_output", duration_ms=round(
            (time.perf_counter() - stage_started) * 1000, 3))
        emit("restore_stage_start", stage="resolve_destination")
        stage_started = time.perf_counter()
        try:
            dest = safe_dest(out_dir, sanitize_filename(store.metadata.name))
        except ValueError as e:
            emit("restore_error", stage="destination", error=str(e))
            return fail(f"还原失败：{e}")
        emit("restore_stage", stage="resolve_destination", duration_ms=round(
            (time.perf_counter() - stage_started) * 1000, 3))
        emit("restore_stage_start", stage="write_output", plain_bytes=len(plain))
        stage_started = time.perf_counter()
        dest.write_bytes(plain)
        emit("restore_stage", stage="write_output", duration_ms=round(
            (time.perf_counter() - stage_started) * 1000, 3),
            plain_bytes=len(plain))
        elapsed = timer.elapsed  # 耗时口径到写盘完成止（sha256 不计入）
        emit("restore_stage_start", stage="sha256", plain_bytes=len(plain))
        stage_started = time.perf_counter()
        sha256 = hashlib.sha256(plain).hexdigest()
        emit("restore_stage", stage="sha256", duration_ms=round(
            (time.perf_counter() - stage_started) * 1000, 3))
        result = ReceiveResult(
            code=0, dest=dest, name=dest.name, plain_size=len(plain),
            elapsed=elapsed, received=store.received_count(),
            total=store.total_frames, sha256=sha256,
        )
        result.warnings.extend(store.warnings)  # meta 落盘降级告警（issue #43）
        if prefetched is not None:
            result.queue_produced = prefetched.stats.produced
            result.queue_dropped = prefetched.stats.dropped
        # payload 清理（issue #12）：还原成功后任务目录不再有续传价值，删除残留；
        # 失败仅告警，不推翻已成功的还原
        try:
            emit("restore_stage_start", stage="cleanup_progress")
            stage_started = time.perf_counter()
            store.cleanup_task()
            emit("restore_stage", stage="cleanup_progress", duration_ms=round(
                (time.perf_counter() - stage_started) * 1000, 3))
        except OSError as e:
            result.warnings.append(f"告警：任务 payload 清理失败（{e}），可手动删除任务目录")
        # 完成通知（issue #12）：通知发送失败只告警；文本用友好大小（issue #25）
        if notify is not None:
            try:
                emit("restore_stage_start", stage="notify")
                stage_started = time.perf_counter()
                notify("文件摆渡还原完成", f"{dest.name}（{friendly_size(len(plain))}）已还原到 {dest.parent}")
                emit("restore_stage", stage="notify", duration_ms=round(
                    (time.perf_counter() - stage_started) * 1000, 3))
            except Exception as e:
                result.warnings.append(f"告警：完成通知发送失败（{e}）")
        emit("receive_done", code=0, received=result.received, total=result.total,
             receive_elapsed_s=result.elapsed, queue_produced=result.queue_produced,
             queue_dropped=result.queue_dropped)
        return result

    if store.data_complete():
        # §4：元数据帧是落盘文件名与还原截断长度的唯一来源，缺失不启动还原
        return fail("元数据缺失：数据帧照常收下，不启动还原")

    return fail("未收齐全部数据帧，不启动还原")
