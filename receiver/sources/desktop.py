"""desktop 取帧源（issue #9）：mss 抓屏 → 稳定闸门 → 统一迭代器接口。

发送页在屏幕上循环播放，本源整屏或按 `--region L,T,W,H` 捕获，经
StableFrameGate（变化检测 + 稳定两帧判定 + 超时强制解码，与 video 源
共用）过滤后按顺序产出 (desktop-NNNNNN, 灰度图)，接入 images 同一条
识别流水线。

DPI 感知（需求 F10）：Windows 下先声明 per-monitor DPI awareness，
mss 才能拿到物理像素坐标，`--region` 与屏幕实际区域逐像素对齐。
"""

import ctypes
import sys

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


def iter_desktop(region: dict | None = None, capture=None):
    """desktop 源迭代器：capture 缺省为 mss 真实抓屏，测试可注入图像序列。"""
    gate = StableFrameGate()
    seq = 0
    for gray in capture if capture is not None else _mss_capture(region):
        out = gate.feed(gray)
        if out is not None:
            seq += 1
            yield f"desktop-{seq:06d}", out
