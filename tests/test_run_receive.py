"""headless 接收核心 run_receive（issue #26）：CLI / tkinter GUI 共用的接收主循环。

缝（issue #26 预定）：
- run_receive 注入边界：reporter_factory / notify / stop_check 可注入，
  返回结构化 ReceiveResult，本模块不打印、不依赖 tty；
- stop_check 协作式停止：已收帧照常持久化（FrameStore 断点续传语义与
  Ctrl+C 一致），不写还原文件、不发完成通知；收齐后才停则照常还原；
- CLI 薄壳（receiver.cli.receive_frames）的文本输出契约由
  test_restore_summary / test_notify 等既有测试守住，此处不重复断言。
"""

import gzip
import hashlib
import os
import zlib
from pathlib import Path

import cv2
import pytest

import fixture_encoder
from receiver import paths
from receiver.run import PrefetchedFrames, ReceiveResult, run_receive


def export_ok(src: bytes, frames_dir: Path, filename: str, file_id: int) -> int:
    """导出完整帧序列，返回数据帧数（末帧可能短于 CHUNK）。"""
    comp = gzip.compress(src)
    fixture_encoder.export_frames(
        comp, file_id, frames_dir,
        filename=filename, plain_size=len(src), cols=48, rows=48, bit=4, pad=3,
    )
    return -(-len(comp) // fixture_encoder.frame_capacity(48, 48))


def iter_frames(frames_dir: Path):
    """images 源等价的 (名称, 灰度图) 迭代器（进程内注入用）。"""
    for p in sorted(frames_dir.glob("*.png")):
        yield p.name, cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)


@pytest.fixture
def progress_root(tmp_path, monkeypatch):
    """进程内测试把任务目录锚点改指 tmp（同 test_notify.py 夹具）。"""
    monkeypatch.setattr(paths, "PROGRESS_DIR", tmp_path / "progress")
    return tmp_path / "progress"


class RecordingReporter:
    """ProgressReporter 同接口替身：记录事件序列，验证 reporter_factory 注入。"""

    def __init__(self):
        self.events = []
        self.entered = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *exc):
        return False

    def set_total(self, total, completed=None):
        self.events.append(("total", total, completed))

    def on_decoded(self, payload, is_new):
        self.events.append(("decoded", is_new, len(payload)))

    def on_rejected(self, name, e):
        self.events.append(("rejected", e.reason))

    def finish(self):
        self.events.append(("finish",))


class RecordingNotifier:
    def __init__(self):
        self.calls = []

    def __call__(self, title, message):
        self.calls.append((title, message))


class ClosableFrames:
    def __init__(self, items):
        self.items = list(items)
        self.closed = False

    def __iter__(self):
        for item in self.items:
            yield item

    def close(self):
        self.closed = True


def test_prefetched_frames_is_bounded_and_counts_drops():
    """C3 预取队列有界，生产过快时明确记录丢弃而不阻塞。"""
    source = ClosableFrames([(str(i), i) for i in range(20)])
    prefetched = PrefetchedFrames(source, maxsize=1)
    prefetched.start()
    # 生产者已结束；队列只需可消费且统计可观测，具体保留哪一帧不构成协议语义。
    items = list(prefetched)
    prefetched.close()
    assert items and items[-1][0].isdigit()
    assert prefetched.stats.produced == 20
    assert prefetched.stats.dropped > 0


def test_prefetched_frames_close_releases_source():
    source = ClosableFrames([])
    prefetched = PrefetchedFrames(source)
    prefetched.close()
    assert source.closed


# ---------- 成功路径：结构化结果字段 ----------

class TestSuccess:
    def test_result_fields_and_dest(self, tmp_path, progress_root):
        """还原成功 → code 0、dest 已写盘、大小/帧数/耗时/sha256 结构化可读。"""
        src = os.urandom(2000)
        file_id = zlib.crc32(src) & 0xFFFFFFFF
        frames_dir = tmp_path / "frames"
        n_data = export_ok(src, frames_dir, "done.bin", file_id)
        notifier = RecordingNotifier()

        result = run_receive(iter_frames(frames_dir), tmp_path / "out",
                             notify=notifier)

        assert isinstance(result, ReceiveResult)
        assert result.code == 0
        assert result.dest is not None and result.dest.name == "done.bin"
        assert result.dest.read_bytes() == src
        assert result.plain_size == len(src)
        assert result.name == "done.bin"
        assert (result.received, result.total) == (n_data, n_data)
        assert result.elapsed > 0, "耗时口径锚点 = 首个新数据帧落地"
        assert result.sha256 == hashlib.sha256(src).hexdigest()
        assert result.error is None
        assert result.incomplete is None
        assert result.warnings == []
        assert result.stopped is False
        assert len(notifier.calls) == 1, "完成通知恰好一次（issue #12 语义不变）"
        # payload 清理（#12）：成功后任务目录无残留
        assert not (progress_root / f"{file_id:08X}").exists()

    def test_reporter_factory_injected(self, tmp_path, progress_root):
        """reporter_factory 注入生效：帧事件走替身，不实例化 CLI rich 进度条。"""
        src = os.urandom(2000)
        file_id = zlib.crc32(src) & 0xFFFFFFFF
        frames_dir = tmp_path / "frames"
        n_data = export_ok(src, frames_dir, "rpt.bin", file_id)
        reporter = RecordingReporter()

        result = run_receive(iter_frames(frames_dir), tmp_path / "out",
                             reporter_factory=lambda: reporter)

        assert result.code == 0
        assert reporter.entered, "reporter 以上下文管理器进入"
        assert reporter.events[-1] == ("finish",)
        assert reporter.events[0][0] == "decoded" and reporter.events[0][1] is False, \
            "首帧为元数据帧：on_decoded(is_new=False)"
        totals = [e for e in reporter.events if e[0] == "total"]
        assert totals and totals[-1][1] == n_data, "set_total 上报总帧数"


# ---------- meta 落盘降级告警的统一出口（issue #43） ----------

class TestStoreWarningsSurfaced:
    def test_meta_warning_surfaces_without_breaking_restore(self, tmp_path, progress_root, monkeypatch):
        """meta.json 落盘失败降级为告警后：还原照常成功（code 0），
        告警经 store.warnings 汇入 ReceiveResult.warnings（CLI/GUI 统一出口）。"""
        import receiver.store as store_mod

        def locked_replace(src, dst):
            raise PermissionError(32, "另一个程序正在使用此文件，进程无法访问。")

        monkeypatch.setattr(store_mod.os, "replace", locked_replace)
        monkeypatch.setattr(store_mod.time, "sleep", lambda _s: None)
        src = os.urandom(2000)
        file_id = zlib.crc32(src) & 0xFFFFFFFF
        frames_dir = tmp_path / "frames"
        export_ok(src, frames_dir, "warn.bin", file_id)

        result = run_receive(iter_frames(frames_dir), tmp_path / "out")

        assert result.code == 0, "meta 是辅助产物，不得覆盖传输成败"
        assert result.dest is not None and result.dest.read_bytes() == src
        assert any("meta.json" in w for w in result.warnings)


# ---------- 协作式停止：stop_check 注入缝 ----------

class TestStopCheck:
    def test_stop_preserves_frames_for_resume(self, tmp_path, progress_root):
        """接收中停止 → stopped 结果、不写还原文件、已收帧持久化保留（可续传）。"""
        src = os.urandom(2000)
        file_id = zlib.crc32(src) & 0xFFFFFFFF
        frames_dir = tmp_path / "frames"
        n_data = export_ok(src, frames_dir, "part.bin", file_id)
        notifier = RecordingNotifier()
        calls = {"n": 0}

        def stop_check():
            calls["n"] += 1
            return calls["n"] > 2  # 元数据帧 + 首个数据帧落地后停

        result = run_receive(iter_frames(frames_dir), tmp_path / "out",
                             notify=notifier, stop_check=stop_check)

        assert result.code == 1
        assert result.stopped is True
        assert result.error is None, "停止不是失败，不产生错误文案"
        assert result.dest is None, "未收齐不还原"
        assert (result.received, result.total) == (1, n_data)
        assert result.incomplete is not None and "还原未完成" in result.incomplete
        assert notifier.calls == [], "停止路径不发完成通知"
        # 已收帧保留（断点续传）：任务目录与 data.bin 在盘
        task_dir = progress_root / f"{file_id:08X}"
        assert task_dir.is_dir()
        assert (task_dir / "data.bin").is_file()
        assert (task_dir / "received.bin").is_file()

    def test_stop_before_first_frame(self, tmp_path, progress_root):
        """首帧前就停 → 已收 0/? 帧口径照常（无帧落地不豁免，同 #25）。"""
        src = os.urandom(2000)
        file_id = zlib.crc32(src) & 0xFFFFFFFF
        frames_dir = tmp_path / "frames"
        export_ok(src, frames_dir, "none.bin", file_id)

        result = run_receive(iter_frames(frames_dir), tmp_path / "out",
                             stop_check=lambda: True)

        assert result.code == 1
        assert result.stopped is True
        assert (result.received, result.total) == (0, None)
        assert "已收 0/? 帧" in result.incomplete

    def test_stop_after_complete_still_restores(self, tmp_path, progress_root):
        """停下的那一刻已收齐 → 照常还原（协作停止不丢弃已到手的完整数据）。"""
        src = os.urandom(2000)
        file_id = zlib.crc32(src) & 0xFFFFFFFF
        frames_dir = tmp_path / "frames"
        export_ok(src, frames_dir, "late.bin", file_id)
        state = {"stopped": False}

        def stop_check():
            # 有限源收齐即 break，stop_check 不再被问；这里恒 False，
            # 由「收齐 break」先到——真正的联合行为用无限源覆盖（ReceiveJob 测试）
            return state["stopped"]

        result = run_receive(iter_frames(frames_dir), tmp_path / "out",
                             stop_check=stop_check)

        assert result.code == 0
        assert result.dest is not None
