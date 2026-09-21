"""camera 取帧源骨架（issue #13）：统一迭代器接口占位。

完整 camera 采集不在本票范围（父 spec Out of Scope）。本模块只接入
sources 统一 (名称, 灰度图) 迭代器约定：后续完整采集扩展时，捕获画面
经 StableFrameGate（与 desktop 源共用）过滤后由本迭代器产出，识别
流水线零改动。
"""

from typing import Iterator

import numpy as np


def iter_camera() -> Iterator[tuple[str, np.ndarray]]:
    """camera 源迭代器：骨架占位，当前不产出任何帧。"""
    yield from ()
