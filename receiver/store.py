"""帧收集与拼装 + 断点续传持久化（issue #7：位图 + 定长文件 + 参数锁定）。

任务目录布局（progress/<FILE_ID 8位大写HEX>/）：
  meta.json     锁定参数 + 元数据帧内容；tmp + os.replace 原子写（N2）
  data.bin      定长 total_frames × chunk_size，payload 按帧号偏移写入（F12）
  received.bin  位图 ceil(total/8) 字节，MSB first，bit i = 帧 i 已收

参数锁定（协议 §6，硬锁）：同一 fileId 首个数据帧落地时锁定 fileId +
几何参数（COLS/ROWS/BIT/PAD）+ chunkSize；任务目录已存在则惰性加载
（位图 + data.bin + meta.json），帧头参数与锁定值不一致 → 整帧拒绝
（FrameRejected('param_lock')），旧进度不可续也不自动作废，须用户手动
清空任务目录后重收。发送端无限循环重播由位图去重吸收（重复帧静默）。

末帧长度：只有末帧允许短于 chunk_size，其有效长度 = 元数据 compressedSize
− (total−1)×chunkSize，在 assemble() 统一截断；元数据缺失时本就不还原。
"""

import json
import os
from collections import OrderedDict
from pathlib import Path

from receiver.metadata import FileMetadata
from receiver.pipeline import DecodedFrame, FrameHeader
from receiver.protocol import FRAME_NO_METADATA, FrameRejected


class IncompleteError(Exception):
    """数据帧未收齐，不能拼装。"""


class FrameStore:
    """按帧号去重收集数据帧并持久化；与锁定值不一致的帧整帧拒绝。

    task_root 非 None 时启用持久化（任务目录 progress/<FILE_ID>/），
    None 时纯内存收集（单测 / 无盘场景）。元数据帧（帧号 0xFFFFFF）
    单独收纳，首帧生效、重复静默忽略（无限重播语义）；还原判据 =
    数据帧齐 ∧ 元数据帧已收（CONTEXT「还原」）。
    """

    def __init__(self, task_root: Path | None = None):
        self._frames: OrderedDict[int, bytes] = OrderedDict()
        self.file_id: int | None = None
        self.total_frames: int | None = None
        self.chunk_size: int | None = None
        self.cols: int | None = None
        self.rows: int | None = None
        self.bit: int | None = None
        self.pad: int | None = None
        self.metadata: FileMetadata | None = None
        self.task_root = task_root
        self.task_dir: Path | None = None
        self._bitmap: bytearray | None = None
        self._data_file = None
        self._metadata_file_id: int | None = None  # 元数据帧所属 fileId（防异帧毒化）

    # ---------- 收帧 ----------

    def add(self, frame: DecodedFrame) -> bool:
        """收下一帧。返回是否为新数据帧；重复帧 / 元数据帧静默忽略（无限重播语义）。"""
        h = frame.header
        if h.frame_no == FRAME_NO_METADATA:
            if self.metadata is not None:
                return False  # 首帧生效，重复（或异帧）静默
            if self.file_id is not None:
                self._validate_against_lock(h)  # 已锁定 → 元数据帧同受硬锁约束
            self.metadata = frame.metadata  # decode_frame 已保证元数据帧携带合法元数据
            self._metadata_file_id = h.file_id
            self._write_meta()
            return False
        if self.file_id is None:
            self._lock(h)
        self._validate_against_lock(h)
        if h.frame_no in self._frames:
            return False
        self._persist_frame(h.frame_no, frame.payload)
        self._frames[h.frame_no] = frame.payload
        return True

    # ---------- 锁定与持久化 ----------

    def _lock(self, h: FrameHeader) -> None:
        """首个数据帧落地：锁定参数；已有任务目录则惰性加载，否则新建。

        锁定值只在确定来源后提交：惰性加载失败（任务目录不完整）抛
        FrameRejected('task_corrupt')，不落任何锁定状态（N2 部分清空防护）。
        """
        if self.task_root is None:
            self._adopt_lock(h)
            return
        self.task_dir = self.task_root / f"{h.file_id:08X}"
        if (self.task_dir / "meta.json").is_file():
            self._lazy_load()
            self._adopt_metadata_for(h)
            return
        # 新任务：先落定长文件与位图，meta.json 原子写最后收口
        self._adopt_lock(h)
        self._adopt_metadata_for(h)
        self.task_dir.mkdir(parents=True, exist_ok=True)
        self._data_file = open(self.task_dir / "data.bin", "wb")
        self._data_file.truncate(self.total_frames * self.chunk_size)
        self._bitmap = bytearray((self.total_frames + 7) // 8)
        (self.task_dir / "received.bin").write_bytes(bytes(self._bitmap))
        self._write_meta()

    def _adopt_lock(self, h: FrameHeader) -> None:
        """以帧头为锁定值提交。"""
        self.file_id = h.file_id
        self.total_frames = h.total_frames
        self.chunk_size = h.chunk_size
        self.cols, self.rows, self.bit, self.pad = h.cols, h.rows, h.bit, h.pad

    def _adopt_metadata_for(self, h: FrameHeader) -> None:
        """锁定时裁决先到的元数据帧：仅同 fileId 者随任务落 meta.json，
        异 fileId 元数据丢弃（防毒化新任务的落盘文件名 / 还原长度）。"""
        if self.metadata is not None and self._metadata_file_id != h.file_id:
            self.metadata = None
            self._metadata_file_id = None

    def _lazy_load(self) -> None:
        """未完成任务惰性加载（N2）：meta.json 锁定值 + 位图 + 已收 payload。

        任务目录不完整（缺文件 / 长度与锁定参数不符，如用户手动部分清空）
        → FrameRejected('task_corrupt')；全部校验通过才提交锁定状态，
        失败时 store 保持未锁定，后续帧重新触发加载并重复告警。
        """
        meta = json.loads((self.task_dir / "meta.json").read_text("utf-8"))
        total = meta["total_frames"]
        chunk = meta["chunk_size"]
        recv_path = self.task_dir / "received.bin"
        data_path = self.task_dir / "data.bin"
        bitmap_len = (total + 7) // 8
        if not (recv_path.is_file() and data_path.is_file()):
            raise FrameRejected("task_corrupt", "任务目录缺少 data.bin / received.bin，须手动清空后重收")
        raw_bitmap = recv_path.read_bytes()
        if len(raw_bitmap) != bitmap_len:
            raise FrameRejected(
                "task_corrupt",
                f"received.bin 长度与位图容量 {bitmap_len} 字节不符（total={total}），须手动清空后重收",
            )
        if data_path.stat().st_size != total * chunk:
            raise FrameRejected(
                "task_corrupt",
                f"data.bin 长度与定长容量 {total * chunk} 字节不符，须手动清空后重收",
            )
        self.file_id = int(meta["file_id"], 16)
        self.total_frames = total
        self.chunk_size = chunk
        self.cols, self.rows = meta["cols"], meta["rows"]
        self.bit, self.pad = meta["bit"], meta["pad"]
        if meta.get("metadata"):
            m = meta["metadata"]
            self.metadata = FileMetadata(
                method=m["method"], plain_size=m["plain_size"],
                compressed_size=m["compressed_size"], name=m["name"],
            )
            self._metadata_file_id = self.file_id
        self._bitmap = bytearray(raw_bitmap)
        self._data_file = open(data_path, "r+b")
        for n in range(total):
            if self._bit_get(n):
                self._frames[n] = self._read_frame(n)

    def _validate_against_lock(self, h: FrameHeader) -> None:
        """帧头 vs 锁定值：跨任务 fileId → 'file_id'；同任务参数漂移 → 硬锁 'param_lock'。"""
        if h.file_id != self.file_id:
            raise FrameRejected(
                "file_id",
                f"0x{h.file_id:08X}（total={h.total_frames}, chunk={h.chunk_size}）"
                f" 与已收任务 0x{self.file_id:08X}（total={self.total_frames}, chunk={self.chunk_size}）不一致",
            )
        mismatch = []
        if h.total_frames != self.total_frames:
            mismatch.append(f"TOTAL_FRAMES {h.total_frames} ≠ 锁定 {self.total_frames}")
        if h.chunk_size != self.chunk_size:
            mismatch.append(f"CHUNK_SIZE {h.chunk_size} ≠ 锁定 {self.chunk_size}")
        if h.cols != self.cols:
            mismatch.append(f"COLS {h.cols} ≠ 锁定 {self.cols}")
        if h.rows != self.rows:
            mismatch.append(f"ROWS {h.rows} ≠ 锁定 {self.rows}")
        if h.bit != self.bit:
            mismatch.append(f"BIT {h.bit} ≠ 锁定 {self.bit}")
        if h.pad != self.pad:
            mismatch.append(f"PAD {h.pad} ≠ 锁定 {self.pad}")
        if mismatch:
            raise FrameRejected(
                "param_lock",
                "；".join(mismatch)
                + f"（任务 0x{self.file_id:08X}）；旧进度不可续，须手动清空任务目录后重收",
            )

    def _persist_frame(self, n: int, payload: bytes) -> None:
        """新帧落盘：先写 data.bin 数据，再置位图收口（崩溃只损失最后一帧标记）。"""
        if self._data_file is None:
            return  # 纯内存模式
        self._data_file.seek(n * self.chunk_size)
        self._data_file.write(payload)
        self._data_file.flush()
        self._bitmap[n >> 3] |= 1 << (7 - (n & 7))
        (self.task_dir / "received.bin").write_bytes(bytes(self._bitmap))

    def _read_frame(self, n: int) -> bytes:
        self._data_file.seek(n * self.chunk_size)
        return self._data_file.read(self.chunk_size)

    def _bit_get(self, n: int) -> int:
        return (self._bitmap[n >> 3] >> (7 - (n & 7))) & 1

    def _write_meta(self) -> None:
        """meta.json 原子写（N2）：tmp + os.replace。锁定时与元数据到达时各写一次。"""
        if self.task_dir is None:
            return
        meta = {
            "file_id": f"{self.file_id:08X}",
            "total_frames": self.total_frames,
            "chunk_size": self.chunk_size,
            "cols": self.cols,
            "rows": self.rows,
            "bit": self.bit,
            "pad": self.pad,
            "metadata": None if self.metadata is None else {
                "method": self.metadata.method,
                "plain_size": self.metadata.plain_size,
                "compressed_size": self.metadata.compressed_size,
                "name": self.metadata.name,
            },
        }
        tmp = self.task_dir / "meta.json.tmp"
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.task_dir / "meta.json")

    def close(self) -> None:
        """释放 data.bin 句柄（Windows 下不关句柄会阻碍任务目录清理）。"""
        if self._data_file is not None:
            self._data_file.close()
            self._data_file = None

    # ---------- 收集判据与拼装 ----------

    def received_count(self) -> int:
        return len(self._frames)

    def data_complete(self) -> bool:
        """数据帧收齐判据：帧号 0..total-1 全部在册。"""
        if self.total_frames is None:
            return False
        return len(self._frames) == self.total_frames and max(self._frames) == self.total_frames - 1

    def is_complete(self) -> bool:
        """还原判据（§4）：数据帧齐 ∧ 元数据帧已收。元数据缺失不启动还原。"""
        return self.data_complete() and self.metadata is not None

    def assemble(self) -> bytes:
        """按帧号序拼接数据区有效字节；末帧按元数据 compressedSize 截断。"""
        if not self.data_complete():
            missing = [n for n in range(self.total_frames) if n not in self._frames]
            raise IncompleteError(f"缺帧 {missing[:10]}")
        if self.metadata is None:
            raise IncompleteError("元数据缺失，无法确定末帧有效长度")
        last = self.total_frames - 1
        last_len = self.metadata.compressed_size - last * self.chunk_size
        if not 1 <= last_len <= self.chunk_size:
            raise IncompleteError(
                f"元数据 compressedSize={self.metadata.compressed_size} 与锁定参数"
                f"（total={self.total_frames}, chunk={self.chunk_size}）不符"
            )
        return b"".join(
            self._frames[n][:last_len] if n == last else self._frames[n]
            for n in range(self.total_frames)
        )
