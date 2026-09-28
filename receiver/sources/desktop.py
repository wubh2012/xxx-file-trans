"""desktop 取帧源（issue #9）：mss / DXGI 抓屏 → 稳定闸门 → 统一迭代器接口。

发送页在屏幕上循环播放，本源整屏或按 `--region L,T,W,H` 捕获，经
StableFrameGate（变化检测 + 稳定两帧判定 + 超时强制解码，与 video 源
共用）过滤后按顺序产出 (desktop-NNNNNN, 灰度图)，接入 images 同一条
识别流水线。

DPI 感知（需求 F10）：Windows 下先声明 per-monitor DPI awareness，
mss 才能拿到物理像素坐标，`--region` 与屏幕实际区域逐像素对齐。

采集后端（issue #31 B2）：`--capture auto`（Windows 优先 DXGI，不可用时
回退 mss）、`mss`（全屏拷贝）或 `dxgi`（Desktop Duplication，经 dxcam；
静屏不重复拷贝、新帧延迟亚毫秒）。
"""

import ctypes
import sys
import threading
import time

import cv2
import numpy as np

from receiver.sources.stable import StableFrameGate


class DXGIRecoveryError(RuntimeError):
    """DXGI 在限定恢复周期内仍不可用，可由 desktop 源回退 mss。"""


# 连续 ACCESS_LOST 会释放并重建 dxcam；超过上限说明显示输出仍在切换，
# 继续自旋只会长期占住接收线程并阻塞正式接收。
DXGI_MAX_RECOVERY_CYCLES = 3
# dxcam 在部分显示输出切换中不抛异常而连续返回 None；超过此窗口视为
# duplication 已失效，触发与异常路径相同的 mss 降级。
DXGI_EMPTY_TIMEOUT_S = 5.0
DXGI_FRAME_STALL_TIMEOUT_S = 5.0
DXGI_SHUTDOWN_TIMEOUT_S = 0.5


def _release_dxgi_camera(camera) -> None:
    """先停 threaded capture 再释放 DXGI 资源，避免残留线程抢占单例。

    解释器关闭期（issue #43）线程创建被禁（RuntimeError，3.13+ 为其子类
    PythonFinalizationError）：回退同步 stop——此刻接收循环已结束，即便
    dxcam 内部采集线程卡住也不再有等待它的消费者；stop 自身再抛错同样
    吞掉，release 照常执行，不覆盖上层原始业务错误。"""
    if camera is None:
        return
    stop = getattr(camera, "stop", None)
    if stop is not None:

        def stop_worker(done: threading.Event):
            try:
                stop()
            except Exception:  # noqa: BLE001 释放阶段不覆盖原始错误
                pass
            finally:
                done.set()

        try:
            done = threading.Event()  # 关闭期线程相关设施一并可能不可用
            threading.Thread(target=stop_worker, args=(done,), daemon=True,
                             name="dxgi-stop").start()
        except RuntimeError:
            try:
                stop()
            except Exception:  # noqa: BLE001 释放阶段不覆盖原始错误
                pass
            try:
                camera.release()
            except Exception:  # noqa: BLE001 释放阶段不覆盖原始错误
                pass
            return
        if not done.wait(DXGI_SHUTDOWN_TIMEOUT_S):
            # dxcam 的内部采集线程可能卡在 AcquireNextFrame；不能让接收
            # 主循环同步等待。旧实例由其 daemon 线程自行结束，当前任务
            # 继续走 mss 回退。
            return
    try:
        camera.release()
    except Exception:  # noqa: BLE001  释放阶段不覆盖原始错误
        pass


def parse_region(text: str) -> dict:
    """`L,T,W,H` → mss 捕获区域 dict。格式或取值非法抛 ValueError（CLI 报用法错误）。"""
    parts = text.split(",")
    if len(parts) != 4:
        raise ValueError(f"--region 需要 L,T,W,H 四个整数，实际 {text!r}")
    try:
        left, top, width, height = (int(p.strip()) for p in parts)
    except ValueError:
        raise ValueError(f"--region 需要 L,T,W,H 四个整数，实际 {text!r}") from None
    if width <= 0 or height <= 0:
        raise ValueError(f"--region 宽高必须为正，实际 {text!r}")
    return {"left": left, "top": top, "width": width, "height": height}


def format_region(region: dict) -> str:
    """mss 区域 dict → `L,T,W,H`（--region 文本格式的逆，与 parse_region 同源）。"""
    return f"{region['left']},{region['top']},{region['width']},{region['height']}"


def _ensure_dpi_awareness() -> None:
    """Windows：per-monitor DPI aware（失败不致命，抓屏仍可用、坐标可能缩放）。"""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


# 模块导入即声明 DPI 感知：CLI 启动后最早的确定时机，先于任何 mss 坐标
# 查询（进程建立后设置虽不保证生效，但声明越早，--region 逐像素对齐越可靠）
_ensure_dpi_awareness()


def _mss_capture(region: dict | None):
    """mss 抓屏适配器：无限产出捕获区域的灰度帧（生成器，随迭代器关闭释放）。

    region 缺省捕获 monitors[0]（全部显示器的虚拟屏并集）：发送页可能
    在任一屏幕播放，全屏并集是唯一不依赖操作员摆放的缺省。
    """
    import mss  # 延迟导入：仅 desktop 真实采集需要，注入 capture 的路径不依赖

    with mss.mss() as sct:
        mon = region if region is not None else sct.monitors[0]
        while True:
            shot = sct.grab(mon)
            yield cv2.cvtColor(np.asarray(shot), cv2.COLOR_BGRA2GRAY)


def _dxgi_capture(region: dict | None,
                  max_recovery_cycles: int | None = None):
    """DXGI Desktop Duplication 采集适配器（issue #31 B2）：DXcam 一次性
    grab 循环。屏幕无更新时 grab 返回 None（不重复全屏拷贝，与 mss 的
    本质差异），此时重发上一帧——保持与 mss 相同的「重复捕获同一画面」
    语义（稳定闸门两帧一致判定依赖它）。

    坐标语义：region 为所选输出（缺省主输出）原点起的物理像素 ltrb；单
    显示器下与 mss 虚拟屏坐标一致，多显示器请用默认 mss 后端（DXGI 的
    输出枚举与显示器坐标映射不在本实现范围）。

    首帧语义：静屏启动时grab 一直为 None，本适配器在首个屏幕更新前不
    产出帧（发送端开播即有更新）；这期间消费端阻塞在迭代器上属预期。

    系统过渡恢复：前台切换、UAC 安全桌面、显示模式变更等会使 duplication
    失效（ACCESS_LOST 0x887A0026 等），dxcam 的 grab 抛异常。适配器先重试
    几次（dxcam 自带 output-change recovery），连续失败才释放并重建相机
    （dxcam 相机按 device/output/backend 单例注册，release 后 create 能
    正确丢弃旧实例）；恢复期重发上一帧维持「重复捕获」语义——单次过渡
    只损失亚秒级画面，不终止接收。
    """
    import dxcam  # 延迟导入：仅 dxgi 后端真实采集需要

    cam_region = None if region is None else (
        region["left"], region["top"],
        region["left"] + region["width"], region["top"] + region["height"])
    camera = None
    last = None
    create_failures = 0
    grab_failures = 0
    recovery_cycles = 0
    empty_since = None
    threaded_capture = False
    frame_stall_since = None
    last_frame_ticks = None
    has_frame_ticks = False
    if max_recovery_cycles is None:
        max_recovery_cycles = DXGI_MAX_RECOVERY_CYCLES
    try:
        while True:
            if camera is None:
                try:
                    camera = dxcam.create(output_color="GRAY", region=cam_region)
                    # dxcam 的 threaded capture 将 DXGI 触碰隔离到其采集线程；
                    # 主线程只从环形缓冲区取最新帧，避免 ACCESS_LOST 时
                    # camera.grab() 把接收主循环永久阻塞。
                    start = getattr(camera, "start", None)
                    if start is not None:
                        start(target_fps=120, video_mode=True)
                        threaded_capture = True
                        has_frame_ticks = hasattr(camera, "latest_frame_ticks")
                    create_failures = 0
                except Exception:  # noqa: BLE001  过渡期重建同样可能失效
                    create_failures += 1
                    if create_failures >= 50:  # ~10s 仍建不起来：明确报错防挂死
                        raise RuntimeError(
                            "dxcam 采集器创建持续失败（区域非法或显示输出不可用）"
                        ) from None
                    time.sleep(0.2)  # 等系统过渡完成再重建
                    continue
            try:
                frame = camera.grab(new_frame_only=True)  # 静屏 None，~0.6ms
                grab_failures = 0
            except Exception:  # noqa: BLE001  duplication 失效（HRESULT 各异）
                grab_failures += 1
                if grab_failures < 5:
                    # dxcam 内部恢复需要几拍：重试期重发上一帧维持语义
                    if last is not None:
                        yield last.reshape(last.shape[0], last.shape[1])
                    time.sleep(0.2)
                    continue
                # 连续失败才重建：先停 threaded capture，再释放单例实例。
                _release_dxgi_camera(camera)
                camera = None
                grab_failures = 0
                recovery_cycles += 1
                if recovery_cycles >= max_recovery_cycles:
                    raise DXGIRecoveryError(
                        "DXGI 显示输出在恢复周期内仍不可用（ACCESS_LOST）；"
                        "将回退 mss 抓屏"
                    ) from None
                time.sleep(0.2)  # 等系统过渡完成再重建
                continue
            if threaded_capture and not getattr(camera, "is_capturing", True):
                raise DXGIRecoveryError(
                    "DXGI threaded capture 线程已停止（ACCESS_LOST）；将回退 mss 抓屏"
                ) from None
            if threaded_capture and has_frame_ticks:
                ticks = getattr(camera, "latest_frame_ticks", None)
                now = time.monotonic()
                if ticks is None or ticks == last_frame_ticks:
                    if frame_stall_since is None:
                        frame_stall_since = now
                    elif now - frame_stall_since >= DXGI_FRAME_STALL_TIMEOUT_S:
                        raise DXGIRecoveryError(
                            "DXGI 最新帧时间戳超过恢复窗口未推进；将回退 mss 抓屏"
                        ) from None
                else:
                    last_frame_ticks = ticks
                    frame_stall_since = now
            if frame is not None:
                last = frame
                recovery_cycles = 0
                empty_since = None
                yield frame.reshape(frame.shape[0], frame.shape[1])  # (H,W,1) → (H,W)
                continue
            if empty_since is None:
                empty_since = time.monotonic()
            elif time.monotonic() - empty_since >= DXGI_EMPTY_TIMEOUT_S:
                raise DXGIRecoveryError(
                    "DXGI 连续空帧超过恢复窗口（ACCESS_LOST）；将回退 mss 抓屏"
                ) from None
            if last is None:
                time.sleep(0.05)  # 首帧未到（静屏）：等屏幕活动
                continue
            yield last.reshape(last.shape[0], last.shape[1])  # 重发上一帧
            time.sleep(0.004)  # 重发节奏钳制（静屏期 ~250Hz 上限，不忙转）
    finally:
        if camera is not None:
            _release_dxgi_camera(camera)


def iter_desktop(region: dict | None = None, capture=None, backend: str = "auto"):
    """desktop 源迭代器：capture 缺省按 backend 真实抓屏（auto / mss / dxgi），
    测试可注入图像序列。关闭时显式 close 内层采集生成器（释放抓屏资源，
    不依赖 GC 终结时机）。"""
    # 30 FPS 时 mss 抓屏 + 解码的串行周期可能大于发送帧周期，稳定两帧
    # 永远无法命中，旧的 5s 超时会表现为“识别卡住”。桌面源的 CRC 已经
    # 会拦截撕裂/过渡画面，因此把强制放行窗口压到 80ms：低 FPS 仍优先
    # 走稳定两帧，高 FPS 则至少每个短窗口把最新候选交给 CRC 判定。
    gate = StableFrameGate(timeout_s=0.08)
    seq = 0
    capture_backend = backend
    if capture is None:
        if backend == "auto":
            # DXGI 仅 Windows 可用；导入失败（未安装 dxcam / 非 Windows）
            # 自动回退 mss，保持原有跨平台行为。
            if sys.platform != "win32":
                backend = "mss"
            else:
                try:
                    import dxcam  # noqa: F401
                except (ImportError, OSError, RuntimeError):
                    backend = "mss"
                else:
                    backend = "dxgi"
        capture = _dxgi_capture(region) if backend == "dxgi" else _mss_capture(region)
        capture_backend = backend
    try:
        while True:
            try:
                for gray in capture:
                    out = gate.feed(gray)
                    if out is not None:
                        seq += 1
                        yield f"desktop-{seq:06d}", out
                return
            except DXGIRecoveryError:
                if capture_backend != "dxgi":
                    raise
                # 旧 duplication 已在 _dxgi_capture 的 finally 中释放；
                # 这里切换到 mss 继续同一闸门和接收任务，不丢整场任务。
                close = getattr(capture, "close", None)
                if close is not None:
                    close()
                capture_backend = "mss"
                capture = _mss_capture(region)
    finally:
        close = getattr(capture, "close", None)
        if close is not None:
            close()
