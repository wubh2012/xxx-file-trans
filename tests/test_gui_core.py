"""GUI 无头逻辑层（issue #26）：命令文本 / 参数校验 / 进度模型 / 事件队列 / 接收线程。

缝（tkinter 薄壳 receiver_gui.pyw 只装配控件，状态与文案全在此模块无头测试，
同 pick.py「纯函数拆出 + 薄壳人工验收」惯例）：
- build_command：GUI 参数 → 与 CLI 等效的完整命令文本（发送端命令助手）；
- validate_config：开始前参数校验文案；
- ProgressModel：typed 帧事件汇总（已收 / 识别率 / 耗时口径与 CLI 一致）；
- QueuedReporter：ProgressReporter 同接口 → typed 事件入队（GUI 主线程消费）；
- ReceiveJob：后台线程 + 协作式停止（images 真帧集成，停止保留已收帧）。
"""

import os
import queue
import shutil
import sys
import time
import zlib
from pathlib import Path

import pytest

from receiver import paths
from receiver.gui_core import (ProgressModel, QueuedReporter, ReceiveJob,
                               build_command, clear_progress, progress_tasks,
                               validate_config)
from receiver.pipeline import FrameRejected

# 夹具与真帧 helper 复用 headless 核心测试（同 test_notify 复用 test_receiver_images 惯例）
from test_run_receive import export_ok, iter_frames, progress_root  # noqa: F401


@pytest.fixture
def progress_root(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "PROGRESS_DIR", tmp_path / "progress")
    return tmp_path / "progress"


# ---------- build_command：命令助手（与 CLI 参数语义一致） ----------

class TestBuildCommand:
    def test_desktop_with_region(self):
        """desktop 源：具体坐标版命令（issue #26 验收），与 parse_region 可回灌。"""
        cmd = build_command("desktop", region={"left": 17, "top": 197,
                                               "width": 1845, "height": 578})
        assert cmd == "python -m receiver receive --source desktop --capture auto --region 17,197,1845,578"

    def test_desktop_fullscreen_omits_region(self):
        """region None（整屏）→ 省略 --region，与 CLI 缺省语义一致。"""
        assert build_command("desktop", region=None) == \
            "python -m receiver receive --source desktop --capture auto"

    def test_images_quotes_path(self):
        """images 源：路径参数加引号（含空格路径可直接粘贴）。"""
        cmd = build_command("images", frames_dir=r"D:\f frames\frames_png")
        assert cmd == 'python -m receiver receive --source images --dir "D:\\f frames\\frames_png"'

    def test_video_quotes_path(self):
        cmd = build_command("video", video=r"D:\rec\screen.mp4")
        assert cmd == 'python -m receiver receive --source video --video "D:\\rec\\screen.mp4"'


# ---------- validate_config：开始前参数校验 ----------

class TestValidateConfig:
    def test_desktop_blank_region_is_fullscreen(self):
        assert validate_config("desktop", region_text="") is None

    def test_desktop_bad_region_format(self):
        err = validate_config("desktop", region_text="1,2,3")
        assert err is not None and "--region" in err

    def test_desktop_nonpositive_region(self):
        err = validate_config("desktop", region_text="17,197,0,100")
        assert err is not None and "宽高" in err

    def test_images_ok(self, tmp_path):
        assert validate_config("images", frames_dir=str(tmp_path)) is None

    def test_images_missing_dir(self, tmp_path):
        err = validate_config("images", frames_dir=str(tmp_path / "nope"))
        assert err is not None and "目录" in err

    def test_images_blank(self):
        err = validate_config("images", frames_dir="")
        assert err is not None

    def test_video_ok(self, tmp_path):
        f = tmp_path / "rec.mp4"
        f.write_bytes(b"x")
        assert validate_config("video", video=str(f)) is None

    def test_video_missing(self, tmp_path):
        err = validate_config("video", video=str(tmp_path / "nope.mp4"))
        assert err is not None


# ---------- ProgressModel：typed 事件 → 进度文案（口径与 CLI 一致） ----------

class TestProgressModel:
    def test_summary_line_fields(self):
        """已收 / 百分比 / 识别率 / 耗时：识别率口径同 CLI（丢弃帧计分母）。"""
        t = {"now": 0.0}
        m = ProgressModel(clock=lambda: t["now"])
        m.on_event(("total", 4, 0))
        t["now"] = 1.0
        m.on_event(("decoded", True, 262))
        m.on_event(("decoded", True, 262))
        m.on_event(("rejected", "crc", "bad frame"))
        t["now"] = 2.0
        m.on_event(("decoded", True, 100))

        line = m.summary_line()
        assert line == "已收 3/4 帧（75%） · 识别率 75.0% · 耗时 1.0 s"
        assert m.elapsed == 2.0 - 1.0, "计时锚点 = 首个 is_new 帧（CLI 同口径）"

    def test_before_first_frame_zero_elapsed(self):
        """首帧落地前耗时 0.0（等待发送端不计入，#25 口径）。"""
        m = ProgressModel(clock=lambda: 100.0)
        m.on_event(("total", 4, 0))
        assert "已收 0/4 帧（0%） · 识别率 100.0% · 耗时 0.0 s" == m.summary_line()

    def test_total_resyncs_received(self):
        """total 事件携带 completed：断点续传重同步（同 ProgressReporter.set_total）。"""
        m = ProgressModel()
        m.on_event(("total", 4, 3))
        assert "已收 3/4 帧（75%）" in m.summary_line()

    def test_unknown_total(self):
        """总帧数未知（首帧未落地）显示 ?。"""
        m = ProgressModel()
        m.on_event(("decoded", True, 10))
        assert "已收 1/? 帧" in m.summary_line()


# ---------- QueuedReporter：reporter 接口 → typed 事件入队 ----------

class TestQueuedReporter:
    def test_events_flow_through_queue(self):
        q = queue.Queue()
        with QueuedReporter(q) as r:
            r.set_total(4, completed=1)
            r.on_decoded(b"x" * 10, True)
            r.on_rejected("f.png", FrameRejected("crc", "校验失败"))
            r.finish()

        events = []
        while not q.empty():
            events.append(q.get_nowait())
        assert events == [
            ("total", 4, 1),
            ("decoded", True, 10),
            ("rejected", "crc", "校验失败"),
            ("finish",),
        ]


def drain_until(q: queue.Queue, pred, timeout=10.0):
    """轮询队列直到谓词命中，返回沿途事件（含命中项）。"""
    deadline = time.monotonic() + timeout
    seen = []
    while time.monotonic() < deadline:
        try:
            ev = q.get(timeout=0.1)
        except queue.Empty:
            continue
        seen.append(ev)
        if pred(ev):
            return seen
    raise AssertionError(f"等待事件超时：已见 {seen}")


# ---------- ReceiveJob：后台线程 + 协作式停止（images 真帧集成） ----------

class TestReceiveJob:
    def test_completes_and_reports(self, tmp_path, progress_root):
        """images 源完整收齐 → done 事件携带成功结果，文件落盘。"""
        src = os.urandom(2000)
        file_id = zlib.crc32(src) & 0xFFFFFFFF
        frames_dir = tmp_path / "frames"
        export_ok(src, frames_dir, "job.bin", file_id)
        q = queue.Queue()
        job = ReceiveJob(lambda: iter_frames(frames_dir), tmp_path / "out", q)

        job.start()
        events = drain_until(q, lambda ev: ev[0] == "done")
        result = events[-1][1]

        assert result.code == 0
        assert result.dest is not None and result.dest.read_bytes() == src
        kinds = [ev[0] for ev in events]
        assert "decoded" in kinds and "total" in kinds, "进度事件经队列上浮"

    def test_cooperative_stop_keeps_frames(self, tmp_path, progress_root):
        """无限重播源 + 停止 → stopped 结果、已收帧保留（可续传）。"""
        src = os.urandom(2000)
        file_id = zlib.crc32(src) & 0xFFFFFFFF
        frames_dir = tmp_path / "frames"
        n_data = export_ok(src, frames_dir, "stop.bin", file_id)

        def endless_frames():
            # 无限重播语义（CONTEXT「断点续传」）：重复帧由位图去重吸收
            while True:
                yield from iter_frames(frames_dir)

        q = queue.Queue()
        job = ReceiveJob(endless_frames, tmp_path / "out", q)
        job.start()
        drain_until(q, lambda ev: ev[0] == "decoded" and ev[1])
        job.stop()
        result = drain_until(q, lambda ev: ev[0] == "done")[-1][1]

        assert result.code == 1
        assert result.stopped is True
        assert result.received >= 1, "停止前至少收到一个新数据帧"
        assert result.dest is None
        task_dir = progress_root / f"{file_id:08X}"
        assert task_dir.is_dir() and (task_dir / "data.bin").is_file()

    def test_source_error_becomes_result(self, tmp_path, progress_root):
        """取帧源打不开等异常 → 兜底错误结果，不击穿线程与 GUI。"""
        q = queue.Queue()
        job = ReceiveJob(lambda: iter_video_missing(tmp_path), tmp_path / "out", q)
        job.start()
        result = drain_until(q, lambda ev: ev[0] == "done")[-1][1]
        assert result.code == 1
        assert result.error and "nope.mp4" in result.error


# ---------- progress_tasks / clear_progress：重新开始（已收进度作废） ----------

class TestClearProgress:
    def test_lists_and_removes_task_dirs(self, progress_root):
        """清空全部任务目录并返回已删目录名；非目录文件不动。"""
        (progress_root / "0DEADBEEF").mkdir(parents=True)
        (progress_root / "0CAFEBABE").mkdir()
        (progress_root / "notes.txt").write_text("x")

        assert [p.name for p in progress_tasks()] == ["0CAFEBABE", "0DEADBEEF"]
        removed = clear_progress()

        assert sorted(removed) == ["0CAFEBABE", "0DEADBEEF"]
        assert progress_tasks() == []
        assert (progress_root / "notes.txt").is_file()

    def test_missing_dir_is_empty(self, tmp_path):
        """progress/ 不存在 = 无任务，空操作。"""
        assert progress_tasks(tmp_path / "nope") == []
        assert clear_progress(tmp_path / "nope") == []

    def test_partial_failure_raises_after_rest(self, progress_root, monkeypatch):
        """个别目录删不掉（句柄占用）：删完其余后抛 OSError，不静默吞错。"""
        (progress_root / "0DEADBEEF").mkdir(parents=True)
        (progress_root / "0CAFEBABE").mkdir()
        real_rmtree = shutil.rmtree

        def flaky_rmtree(path, *a, **kw):
            if Path(path).name == "0DEADBEEF":
                raise OSError("句柄占用")
            return real_rmtree(path, *a, **kw)

        monkeypatch.setattr("receiver.gui_core.shutil.rmtree", flaky_rmtree)
        with pytest.raises(OSError) as ei:
            clear_progress()
        assert "0DEADBEEF" in str(ei.value)
        assert not (progress_root / "0CAFEBABE").exists()


def iter_video_missing(tmp_path):
    from receiver.sources.video import iter_video
    return iter_video(tmp_path / "nope.mp4")  # 生成器惰性打开，首次迭代抛 ValueError


# ---------- tkinter 薄壳冒烟（构造 + 切源 + 命令联动，不进 mainloop） ----------

class TestTkinterShellSmoke:
    @pytest.fixture
    def shell(self):
        tk = pytest.importorskip("tkinter")
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "receiver_gui_shell",
            Path(__file__).resolve().parent.parent / "receiver_gui.pyw")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["receiver_gui_shell"] = mod  # _restart 测试按名取模块打桩
        spec.loader.exec_module(mod)  # 仅导入装配层，不进 mainloop
        try:
            root = tk.Tk()
        except tk.TclError:
            pytest.skip("无显示环境")
        root.withdraw()
        try:
            yield mod.ReceiverGui(root)
        finally:
            root.destroy()

    def test_constructs_with_desktop_command(self, shell):
        """初始 desktop 源：窗口构造即出整屏命令（无坐标省略 --region）。"""
        assert shell.command_var.get() == \
            "python -m receiver receive --source desktop --capture auto"

    def test_pick_fill_text_drives_command(self, shell):
        """框选回填（等价于手填坐标）→ 命令带具体坐标，与实际参数一致。"""
        shell.region_var.set("17,197,1845,578")
        assert shell.command_var.get() == \
            "python -m receiver receive --source desktop --capture auto --region 17,197,1845,578"

    def test_source_switch_and_images_command(self, shell, tmp_path):
        """切 images 源 + 填目录 → 参数帧切换、命令联动（含引号路径）。"""
        shell._select_source("images")
        shell.dir_var.set(str(tmp_path))
        assert shell.command_var.get() == \
            f'python -m receiver receive --source images --dir "{tmp_path}"'

    def test_restart_button_in_controls(self, shell):
        """重新开始按钮与开始接收并排，接收中一并置灰。"""
        assert shell.restart_btn in shell._controls
        shell._set_setup_state("disabled")
        assert str(shell.restart_btn["state"]) == "disabled"

    def test_restart_clears_then_starts(self, shell, progress_root, monkeypatch):
        """确认 → 清空已收进度 → 复用 _start（不续传）。"""
        mod = sys.modules["receiver_gui_shell"]
        (progress_root / "0DEADBEEF").mkdir(parents=True)
        calls = []
        monkeypatch.setattr(mod, "clear_progress", lambda: calls.append("clear"))
        monkeypatch.setattr(mod.messagebox, "askyesno", lambda *a, **k: True)
        monkeypatch.setattr(shell, "_start", lambda: calls.append("start"))
        shell._restart()
        assert calls == ["clear", "start"]

    def test_restart_cancelled_is_noop(self, shell, monkeypatch):
        """确认框取消 → 不清进度、不开始。"""
        mod = sys.modules["receiver_gui_shell"]
        calls = []
        monkeypatch.setattr(mod, "clear_progress", lambda: calls.append("clear"))
        monkeypatch.setattr(mod.messagebox, "askyesno", lambda *a, **k: False)
        monkeypatch.setattr(shell, "_start", lambda: calls.append("start"))
        shell._restart()
        assert calls == []


# ---------- video 源片模式（issue #45） ----------

class TestVideoTapeMode:
    def test_build_command_tape_appends_flag(self):
        cmd = build_command("video", video="t.mp4", tape=True)
        assert cmd.endswith(" --tape")
        assert " --tape" not in build_command("video", video="t.mp4")

    def test_make_frames_passes_tape_to_iter_video(self, monkeypatch):
        import receiver.gui_core as gc

        seen = {}

        def fake_iter_video(video, *, tape=False):
            seen["tape"] = tape
            yield "video-000001", None

        monkeypatch.setattr(gc, "iter_video", fake_iter_video)
        list(gc.make_frames("video", video="t.mp4"))
        assert seen["tape"] is False
        list(gc.make_frames("video", video="t.mp4", tape=True))
        assert seen["tape"] is True
