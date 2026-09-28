"""video 取帧源（issue #10）：预录视频解帧 → 稳定闸门 → 统一迭代器接口。

解析预先录制的发送端屏幕视频文件（离线复盘与批量验证），逐帧解码后
经 StableFrameGate（变化检测 + 稳定两帧判定 + 超时强制解码，与 desktop
源共用）过滤，按顺序产出 (video-NNNNNN, 灰度图)，接入 images 同一条
识别流水线。

时间戳注入确定性时钟（stable.py 约定）：now = 帧序号 / 录制帧率，
闸门的超时窗随视频播放时间推进，与离线解码快慢无关。

带模式（tape=True，issue #45）：旁路稳定闸门逐帧直读。制带 MP4
（tapemaker，ADR-0003）每个传输帧恰出现一次，相邻帧内容全部不同，
「稳定两帧」判定永不满足、只能靠超时兜底放行——闸门的三个判定对带
均无对象（无过渡画面；重复帧由接收端帧号幂等落盘吸收；噪点帧由 CRC
整帧丢弃兜底）。
"""

from typing import Iterator

import cv2
import numpy as np

from receiver.sources.stable import StableFrameGate

_DEFAULT_FPS = 30.0  # 容器缺失帧率元数据时的兜底


def iter_video(video, *, tape: bool = False) -> Iterator[tuple[str, np.ndarray]]:
    """video 源迭代器：逐帧解码预录视频；默认经稳定闸门过滤后放行，
    tape=True（带模式）逐帧直读（issue #45）。"""
    gate = None if tape else StableFrameGate()
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise ValueError(f"无法打开视频文件：{video}")
    fps = cap.get(cv2.CAP_PROP_FPS)
    if not fps or fps <= 0:
        fps = _DEFAULT_FPS
    seq = 0  # 放行序号（与 desktop 源一致，按放行顺序连续编号）
    capture_idx = 0  # 解码序号（确定性时钟：now = 解码序号 / 录制帧率）
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            out = gray if gate is None else gate.feed(gray, now=capture_idx / fps)
            capture_idx += 1
            if out is not None:
                seq += 1
                yield f"video-{seq:06d}", out
        if gate is None:
            return
        # 流末 flush：最后一个传输帧可能只被解码一次（录制截尾），
        # 作为滞留候选收尾放行，不静默丢失
        out = gate.flush()
        if out is not None:
            seq += 1
            yield f"video-{seq:06d}", out
    finally:
        cap.release()
