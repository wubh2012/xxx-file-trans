"""完成通知 + payload 清理（issue #12）。

三条缝：
- FrameStore 公共接口：cleanup_task() 清理任务目录 payload 残留
  （参照 tests/test_resume.py 的直接单测惯例）；
- 通知缝：receive_frames(..., notify=...) 可注入边界——桌面通知无法
  在测试中真实弹出，测试注入 fake 断言「还原成功时被调用、失败时
  不被调用」；真实 winotify 实现尽量薄（receiver/notify.py）；
- 缝 B：CLI 子进程外沿（还原成功后 progress/<FILE_ID>/ 任务目录被清空）。

验收（issue #12）：
- 完成时 Windows 桌面通知 + 声音（winotify，薄实现不测真实弹窗）；
- 缺少通知依赖时自动降级为终端高亮提示；
- 还原成功后该任务 payload 自动清理。
"""

import gzip
import io
import os
import shutil
import sys
import types
import zlib
from argparse import Namespace
from pathlib import Path

import pytest

import fixture_encoder
from receiver import notify as notify_mod
from receiver import paths
from receiver.cli import receive_frames
from receiver.sources import iter_source
from receiver.store import FrameStore
from test_resume import data_frame, task_dir
from test_receiver_images import geometry, run_receive


class RecordingNotifier:
    """注入 receive_frames 的 fake：记录调用，供成功/失败路径断言。"""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def __call__(self, title: str, message: str) -> None:
        self.calls.append((title, message))


# ---------- payload 清理：FrameStore.cleanup_task() ----------

class TestPayloadCleanup:
    def test_cleanup_removes_task_dir(self, tmp_path):
        """还原成功后 cleanup_task() 删除任务目录（data.bin / received.bin /
        meta.json payload 残留一并清除），磁盘不被已还原任务撑爆。"""
        store = FrameStore(tmp_path)
        store.add(data_frame(0, 4, 100))
        store.close()

        tdir = task_dir(tmp_path, 0xDEADBEEF)
        assert tdir.is_dir()

        store.cleanup_task()

        assert not tdir.exists(), "任务目录应整体清除"

    def test_cleanup_keeps_other_tasks(self, tmp_path):
        """只清理本任务目录，其他任务（其他 fileId）不受影响。"""
        other = FrameStore(tmp_path)
        other.add(data_frame(0, 4, 100, file_id=0xAAAA0001))
        other.close()
        mine = FrameStore(tmp_path)
        mine.add(data_frame(0, 4, 100, file_id=0xAAAA0002))
        mine.close()

        mine.cleanup_task()

        assert task_dir(tmp_path, 0xAAAA0001).is_dir(), "其他任务目录不得误删"
        assert not task_dir(tmp_path, 0xAAAA0002).exists()

    def test_cleanup_without_task_dir_is_noop(self):
        """纯内存收集（task_root=None）无任务目录，清理不得抛错。"""
        store = FrameStore(None)
        store.add(data_frame(0, 4, 100))
        store.cleanup_task()


# ---------- 通知缝：receive_frames 成功调用 / 失败不调用 ----------

def export_ok(src: bytes, frames_dir: Path, filename: str) -> list[Path]:
    comp = gzip.compress(src)
    return fixture_encoder.export_frames(
        comp, zlib.crc32(src) & 0xFFFFFFFF, frames_dir,
        filename=filename, plain_size=len(src), **geometry(),
    )


def receive(args_out: Path, frames_dir: Path, notify) -> int:
    """进程内直跑接收主循环（progress/ 目录锚点指向 tmp，避免污染锚定目录）。"""
    args = Namespace(out=args_out)
    return receive_frames(args, iter_source("images", frames_dir), notify=notify)


@pytest.fixture
def progress_root(tmp_path, monkeypatch):
    """进程内测试把任务目录锚点改指 tmp，用后清理（同 test_resume 缝 B 夹具）。"""
    monkeypatch.setattr(paths, "PROGRESS_DIR", tmp_path / "progress")
    yield tmp_path / "progress"


class TestNotifyOnRestore:
    def test_notify_called_on_successful_restore(self, tmp_path, progress_root):
        """还原成功 → 注入的通知边界被调用，消息含落盘文件名与友好大小
        （issue #25：通知文本不再出现裸字节数）。"""
        src = os.urandom(2000)
        frames_dir = tmp_path / "frames"
        export_ok(src, frames_dir, "done.bin")
        notifier = RecordingNotifier()

        rc = receive(tmp_path / "out", frames_dir, notifier)

        assert rc == 0
        assert len(notifier.calls) == 1, f"成功应恰好通知一次：{notifier.calls}"
        title, message = notifier.calls[0]
        assert "done.bin" in message
        assert "2.0 KiB" in message, f"通知应使用友好大小：{message}"
        assert "字节" not in message and str(len(src)) not in message, \
            f"通知不得出现裸字节数：{message}"

    def test_notify_not_called_on_incomplete(self, tmp_path, progress_root):
        """未收齐不还原 → 通知边界不得被调用（失败不报喜）。"""
        src = os.urandom(2000)
        frames_dir = tmp_path / "frames"
        paths_all = export_ok(src, frames_dir, "part.bin")
        paths_all[-1].unlink()
        notifier = RecordingNotifier()

        rc = receive(tmp_path / "out", frames_dir, notifier)

        assert rc == 1
        assert notifier.calls == [], "失败路径不得通知"
        assert (progress_root / f"{zlib.crc32(src) & 0xFFFFFFFF:08X}").is_dir(), \
            "失败路径任务目录保留（断点续传）"

    def test_payload_cleaned_after_successful_restore(self, tmp_path, progress_root):
        """还原成功 → 该任务 payload 残留（progress/<FILE_ID>/）自动清理。"""
        src = os.urandom(2000)
        frames_dir = tmp_path / "frames"
        export_ok(src, frames_dir, "clean.bin")

        rc = receive(tmp_path / "out", frames_dir, RecordingNotifier())

        assert rc == 0
        fid = f"{zlib.crc32(src) & 0xFFFFFFFF:08X}"
        assert not (progress_root / fid).exists(), "已还原任务目录应自动清理"

    def test_cleanup_failure_warns_but_restore_still_succeeds(self, tmp_path, progress_root, capsys, monkeypatch):
        """清理失败（如句柄占用）：还原结果不受影响（rc=0），但告警可见，
        不静默吞错（磁盘残留须可感知）。"""
        src = os.urandom(2000)
        frames_dir = tmp_path / "frames"
        export_ok(src, frames_dir, "stubborn.bin")

        def boom(path, *a, **kw):
            raise PermissionError(32, "句柄占用", str(path))
        monkeypatch.setattr("receiver.store.shutil.rmtree", boom)

        rc = receive(tmp_path / "out", frames_dir, RecordingNotifier())

        assert rc == 0, "清理失败不得推翻已成功的还原"
        assert "清理失败" in capsys.readouterr().err

    def test_notify_failure_warns_but_restore_still_succeeds(self, tmp_path, progress_root, capsys):
        """通知发送失败（如通知服务异常）：不得推翻已成功的还原（rc=0），
        告警可见。"""
        src = os.urandom(2000)
        frames_dir = tmp_path / "frames"
        export_ok(src, frames_dir, "loud.bin")

        def broken_notify(title, message):
            raise RuntimeError("通知服务不可用")

        rc = receive(tmp_path / "out", frames_dir, broken_notify)

        assert rc == 0, "通知失败不得推翻已成功的还原"
        assert "通知" in capsys.readouterr().err


# ---------- 通知工厂：缺 winotify 降级终端高亮（可选依赖不崩） ----------

def fake_winotify(monkeypatch, record: list):
    """向 sys.modules 注入假 winotify（外部依赖边界替身），记录调用序列。"""

    class FakeNotification:
        def __init__(self, app_id, title, msg):
            record.append(("init", app_id, title, msg))

        def set_audio(self, sound, loop):
            record.append(("audio", sound, loop))

        def show(self):
            record.append(("show",))

    fake_audio = types.SimpleNamespace(Default="default-sound")
    mod = types.ModuleType("winotify")
    mod.Notification = FakeNotification
    mod.audio = fake_audio
    monkeypatch.setitem(sys.modules, "winotify", mod)


class TestNotifierFactory:
    def test_degrades_to_terminal_without_winotify(self, monkeypatch):
        """winotify 不可用 → 降级为终端提示实现，工厂不得抛 ImportError。"""
        monkeypatch.setitem(sys.modules, "winotify", None)  # import 即 ImportError

        notifier = notify_mod.default_notifier()

        assert isinstance(notifier, notify_mod.TerminalNotifier)

    def test_picks_desktop_when_winotify_present(self, monkeypatch):
        """winotify 可用 → 返回桌面通知实现。"""
        fake_winotify(monkeypatch, [])

        notifier = notify_mod.default_notifier()

        assert isinstance(notifier, notify_mod.DesktopNotifier)

    def test_terminal_notifier_prints_highlighted(self):
        """降级路径：终端提示包含标题与消息（rich 高亮关闭标记解析）。"""
        buf = io.StringIO()
        from rich.console import Console
        notifier = notify_mod.TerminalNotifier(console=Console(file=buf, width=200))

        notifier("文件摆渡还原完成", "done.bin（1234 字节）")

        out = buf.getvalue()
        assert "文件摆渡还原完成" in out
        assert "done.bin（1234 字节）" in out

    def test_desktop_notifier_shows_with_sound(self, monkeypatch):
        """真实实现薄适配：构造通知并设置声音后 show（标题 / 消息透传）。"""
        record = []
        fake_winotify(monkeypatch, record)

        notify_mod.DesktopNotifier()("文件摆渡还原完成", "done.bin（1234 字节）")

        kinds = [r[0] for r in record]
        assert kinds == ["init", "audio", "show"], \
            f"应构造通知、设置声音并弹出：{record}"
        assert record[0][2] == "文件摆渡还原完成"
        assert record[0][3] == "done.bin（1234 字节）"


# ---------- CLI 默认接线 + 缝 B：子进程外沿成功后清理任务目录 ----------

class TestCliWiring:
    def test_main_uses_default_notifier(self, tmp_path, monkeypatch, progress_root):
        """CLI 默认路径走 default_notifier() 工厂（还原成功 → 通知被发出）。"""
        src = os.urandom(2000)
        frames_dir = tmp_path / "frames"
        export_ok(src, frames_dir, "wired.bin")
        notifier = RecordingNotifier()
        import receiver.cli as cli_mod
        monkeypatch.setattr(cli_mod, "default_notifier", lambda: notifier)

        rc = cli_mod.main(["receive", "--source", "images", "--dir", str(frames_dir),
                           "--out", str(tmp_path / "out")])

        assert rc == 0
        assert len(notifier.calls) == 1, f"默认接线应发出完成通知：{notifier.calls}"


@pytest.fixture
def anchored_progress():
    """缝 B 任务落在锚定 progress/，用后清理（同 test_resume）。"""
    yield paths.PROGRESS_DIR
    shutil.rmtree(paths.PROGRESS_DIR, ignore_errors=True)


class TestCliEndToEnd:
    def test_success_cleans_task_dir(self, tmp_path, anchored_progress):
        """缝 B：完整重播还原成功（rc=0）→ progress/<FILE_ID>/ 被清理；
        首轮只播前半段（rc=1）时任务目录先落盘（对照）。"""
        src = os.urandom(2000)
        full = tmp_path / "full"
        paths_all = export_ok(src, full, "e2e.bin")
        partial = tmp_path / "partial"
        shutil.copytree(full, partial)
        for p in paths_all[len(paths_all) // 2:]:
            (partial / p.name).unlink()
        out = tmp_path / "output"
        fid = f"{zlib.crc32(src) & 0xFFFFFFFF:08X}"

        assert run_receive(partial, out).returncode == 1
        assert (paths.PROGRESS_DIR / fid).is_dir(), "未收齐时任务目录保留"

        r2 = run_receive(full, out)
        assert r2.returncode == 0, f"stdout={r2.stdout}\nstderr={r2.stderr}"
        assert not (paths.PROGRESS_DIR / fid).exists(), "还原成功后任务目录应清理"
