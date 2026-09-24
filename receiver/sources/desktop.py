"""desktop 取帧源（issue #9）：mss / DXGI 抓屏 → 稳定闸门 → 统一迭代器接口。

发送页在屏幕上循环播放，本源整屏或按 `--region L,T,W,H` 捕获，经
StableFrameGate（变化检测 + 稳定两帧判定 + 超时强制解码，与 video 源
共用）过滤后按顺序产出 (desktop-NNNNNN, 灰度图)，接入 images 同一条
识别流水线。

DPI 感知（需求 F10）：Windows 下先声明 per-monitor DPI awareness，
mss 才能拿到物理像素坐标，`--region` 与屏幕实际区域逐像素对齐。

采集后端（issue #31 B2）：`--capture mss`（默认，全屏拷贝）或 `dxgi`
（Desktop Duplication，经 dxcam；静屏不重复拷贝、新帧延迟亚毫秒）。
"""

import ctypes
import sys
import time

import cv2
import numpy as np

from receiver.sources.stable import StableFrameGate


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


def _dxgi_capture(region: dict | None):
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
    try:
        while True:
            if camera is None:
                try:
                    camera = dxcam.create(output_color="GRAY", region=cam_region)
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
                try:  # 连续失败才重建：release 后单例注册表丢弃旧实例
                    camera.release()
                except Exception:  # noqa: BLE001  释放失败不阻断重建
                    pass
                camera = None
                grab_failures = 0
                time.sleep(0.2)  # 等系统过渡完成再重建
                continue
            if frame is not None:
                last = frame
                yield frame.reshape(frame.shape[0], frame.shape[1])  # (H,W,1) → (H,W)
                continue
            if last is None:
                time.sleep(0.05)  # 首帧未到（静屏）：等屏幕活动
                continue
            yield last.reshape(last.shape[0], last.shape[1])  # 重发上一帧
            time.sleep(0.004)  # 重发节奏钳制（静屏期 ~250Hz 上限，不忙转）
    finally:
        if camera is not None:
            camera.release()


def iter_desktop(region: dict | None = None, capture=None, backend: str = "mss"):
    """desktop 源迭代器：capture 缺省按 backend 真实抓屏（mss / dxgi），
    测试可注入图像序列。关闭时显式 close 内层采集生成器（释放抓屏资源，
    不依赖 GC 终结时机）。"""
    gate = StableFrameGate()
    seq = 0
    if capture is None:
        capture = _dxgi_capture(region) if backend == "dxgi" else _mss_capture(region)
    try:
        for gray in capture:
            out = gate.feed(gray)
            if out is not None:
                seq += 1
                yield f"desktop-{seq:06d}", out
    finally:
        close = getattr(capture, "close", None)
        if close is not None:
            close()
