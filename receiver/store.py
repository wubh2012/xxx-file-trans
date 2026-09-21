"""帧收集与拼装（issue #4 最小内存态）。

received.bin 位图 + data.bin 定长持久化、参数锁定硬锁属 issue #7
（断点续传），届时在 add()/懒加载处扩展，收集接口不变。
"""

from collections import OrderedDict

from receiver.pipeline import DecodedFrame
from receiver.protocol import FRAME_NO_METADATA, FrameRejected


class IncompleteError(Exception):
    """数据帧未收齐，不能拼装。"""


class FrameStore:
    """按帧号去重收集数据帧；FILE_ID / 帧总数 / 分片大小不一致的帧整帧拒绝
    （跨任务不混收；完整参数锁定硬锁属 issue #7）。"""

    def __init__(self):
        self._frames: OrderedDict[int, DecodedFrame] = OrderedDict()
        self.file_id: int | None = None
        self.total_frames: int | None = None
        self.chunk_size: int | None = None

    def add(self, frame: DecodedFrame) -> bool:
        """收下一帧。返回是否为新帧；重复帧静默忽略（无限重播语义）。"""
        h = frame.header
        if h.frame_no == FRAME_NO_METADATA:
            raise FrameRejected("metadata", "元数据帧处理属 issue #6，本票整帧跳过")
        if self.file_id is None:
            self.file_id = h.file_id
            self.total_frames = h.total_frames
            self.chunk_size = h.chunk_size
        elif (
            h.file_id != self.file_id
            or h.total_frames != self.total_frames
            or h.chunk_size != self.chunk_size
        ):
            raise FrameRejected(
                "file_id",
                f"0x{h.file_id:08X}（total={h.total_frames}, chunk={h.chunk_size}）"
                f" 与已收任务 0x{self.file_id:08X}（total={self.total_frames}, chunk={self.chunk_size}）不一致",
            )
        if h.frame_no in self._frames:
            return False
        self._frames[h.frame_no] = frame
        return True

    def received_count(self) -> int:
        return len(self._frames)

    def is_complete(self) -> bool:
        """收齐判据（本票）：帧号 0..total-1 全部在册（元数据判据属 issue #6）。"""
        if self.total_frames is None:
            return False
        return len(self._frames) == self.total_frames and max(self._frames) == self.total_frames - 1

    def assemble(self) -> bytes:
        """按帧号序拼接数据区有效字节。"""
        if not self.is_complete():
            missing = [n for n in range(self.total_frames) if n not in self._frames]
            raise IncompleteError(f"缺帧 {missing[:10]}")
        return b"".join(self._frames[n].payload for n in range(self.total_frames))
