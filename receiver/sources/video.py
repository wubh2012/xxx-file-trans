"""视频文件逐帧解码，产出 (video-NNNNNN, 灰度图)。

有限视频里的有效画面可能只出现一次（制片、录屏或平台转码），不能
要求连续两次捕获一致。所有帧交给识别流水线：坏帧由协议校验拒绝，
重复帧由接收端按帧号幂等处理。稳定闸门仅适用于实时采集源。
"""

from typing import Iterator

import cv2
import numpy as np


def iter_video(video, *, tape: bool = False) -> Iterator[tuple[str, np.ndarray]]:
    """逐帧读取视频；tape 参数保留兼容，开关均使用逐帧路径。"""
    cap = cv2.VideoCapture(str(video))
    try:
        if not cap.isOpened():
            raise ValueError(f"无法打开视频文件：{video}")
        seq = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            seq += 1
            yield f"video-{seq:06d}", cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    finally:
        cap.release()
