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


def run_receive(frames, out_dir: Path, reporter_factory=ProgressReporter,
                notify=None, stop_check=None) -> ReceiveResult:
    """接收主循环：frames 为任一取帧源的 (名称, 灰度图) 迭代器。

    reporter_factory 返回 ProgressReporter 同接口对象（上下文管理器 +
    on_decoded / on_rejected / set_total / finish，issue #26 注入缝）；
    stop_check 非 None 时逐帧调用，返回真即协作式停止。
    """
    out_dir = Path(out_dir)
    # 断点续传（issue #7）：任务目录锚定 progress/，首个数据帧落地即锁定，
    # 崩溃 / Ctrl+C 重启后惰性加载已收帧，只补缺失帧
    store = FrameStore(paths.PROGRESS_DIR)
    timer = RestoreTimer()  # 还原计时（issue #25）：锚点 = 首个 is_new 数据帧落地

    def fail(msg: str | None, *, stopped: bool = False) -> ReceiveResult:
        """统一未完成出口：失败原因 + 未完成统计（issue #25），退出码 1。"""
        return ReceiveResult(
            code=1, stopped=stopped, error=msg,
            received=store.received_count(), total=store.total_frames,
            incomplete=incomplete_summary(timer.elapsed,
                                          store.received_count(), store.total_frames),
        )

    stopped = False
    geo_cache = GeometryCache()  # 角标检测缓存（issue #30 C1）：会话内帧间几何不变
    try:
        with reporter_factory() as reporter:
            try:
                for name, img in frames:
                    if stop_check is not None and stop_check():
                        stopped = True
                        break
                    try:
                        frame: DecodedFrame = decode_frame(img, geo_cache)
                    except FrameRejected as e:
                        reporter.on_rejected(name, e)
                        continue
                    try:
                        is_new = store.add(frame)
                    except FrameRejected as e:
                        # 跨任务混帧 / 参数锁定硬锁（param_lock）同样整帧拒绝
                        reporter.on_rejected(name, e)
                        continue
                    if is_new:
                        timer.start()  # 与参数锁定同点起算，首帧前时间不计入
                    reporter.on_decoded(frame.payload, is_new)
                    if store.total_frames is not None:
                        reporter.set_total(store.total_frames,
                                           completed=store.received_count())
                    if store.is_complete():
                        # 收齐判据满足即提前退出进入还原（F13/验收标准 2，
                        # issue #19）：desktop 等无限源不等流耗尽。元数据
                        # 缺失时永不 complete，照常继续收帧。
                        break
            except KeyboardInterrupt:
                return fail("接收中断（Ctrl+C）：进度已持久化，重新运行将只补缺失帧")
            reporter.finish()
    finally:
        store.close()

    if stopped and not store.is_complete():
        # 协作式停止（issue #26）：停止不是失败（error 置空），已收帧保留可续传
        return fail(None, stopped=True)

    if store.is_complete():
        try:
            plain = gunzip_verify(store.assemble())
        except (IncompleteError, RestoreError) as e:
            return fail(f"还原失败：{e}")
        if len(plain) != store.metadata.plain_size:
            return fail(
                f"还原失败：plainSize 不一致（元数据声明 {store.metadata.plain_size}，"
                f"实际解压 {len(plain)}）"
            )
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            dest = safe_dest(out_dir, sanitize_filename(store.metadata.name))
        except ValueError as e:
            return fail(f"还原失败：{e}")
        dest.write_bytes(plain)
        elapsed = timer.elapsed  # 耗时口径到写盘完成止（sha256 不计入）
        result = ReceiveResult(
            code=0, dest=dest, name=dest.name, plain_size=len(plain),
            elapsed=elapsed, received=store.received_count(),
            total=store.total_frames, sha256=hashlib.sha256(plain).hexdigest(),
        )
        # payload 清理（issue #12）：还原成功后任务目录不再有续传价值，删除残留；
        # 失败仅告警，不推翻已成功的还原
        try:
            store.cleanup_task()
        except OSError as e:
            result.warnings.append(f"告警：任务 payload 清理失败（{e}），可手动删除任务目录")
        # 完成通知（issue #12）：通知发送失败只告警；文本用友好大小（issue #25）
        if notify is not None:
            try:
                notify("文件摆渡还原完成", f"{dest.name}（{friendly_size(len(plain))}）已还原到 {dest.parent}")
            except Exception as e:
                result.warnings.append(f"告警：完成通知发送失败（{e}）")
        return result

    if store.data_complete():
        # §4：元数据帧是落盘文件名与还原截断长度的唯一来源，缺失不启动还原
        return fail("元数据缺失：数据帧照常收下，不启动还原")

    return fail("未收齐全部数据帧，不启动还原")
