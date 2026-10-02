"""采集后端分派及失败帧诊断留样，不依赖实际桌面。"""

import json

import cv2
import numpy as np

from receiver.gui_core import JsonlTraceWriter, build_command, make_frames


def test_explicit_mss_is_used_by_gui_and_command(monkeypatch):
    import receiver.gui_core as core

    seen = {}

    def capture(**kwargs):
        seen.update(kwargs)
        return iter([])

    monkeypatch.setattr(core, "iter_desktop", capture)
    list(make_frames("desktop", capture="mss"))
    assert seen["backend"] == "mss"
    assert "--capture mss" in build_command("desktop", capture="mss")


def test_failed_frame_is_saved_by_actual_receive_loop(tmp_path, monkeypatch):
    import receiver.paths as paths

    monkeypatch.setattr(paths, "PROGRESS_DIR", tmp_path / "progress")
    trace = JsonlTraceWriter(tmp_path / "receive.jsonl", save_rejected_frames=True)
    img = np.zeros((40, 60), np.uint8)
    try:
        from receiver.run import run_receive
        result = run_receive([("desktop-1", img)], tmp_path / "out",
                             trace=trace, allow_scaled=True)
    finally:
        trace.close()
    assert result.code == 1
    events = [json.loads(line) for line in trace.path.read_text(encoding="utf-8").splitlines()]
    snapshot = next(event for event in events if event["event"] == "frame_snapshot")
    assert np.array_equal(cv2.imread(snapshot["path"], cv2.IMREAD_GRAYSCALE), img)


def test_diagnostic_snapshots_are_bounded_and_spaced(tmp_path, monkeypatch):
    import receiver.gui_core as core

    ticks = iter([0, 0, 0, 0, 2, 2, 4, 4, 6])
    monkeypatch.setattr(core.time, "monotonic", lambda: next(ticks))
    trace = JsonlTraceWriter(tmp_path / "receive.jsonl", save_rejected_frames=True)
    img = np.zeros((40, 60), np.uint8)
    try:
        for _ in range(5):
            trace.save_rejected_frame(img, "geometry")
    finally:
        trace.close()
    assert len(list(trace.path.with_suffix("").glob("*.png"))) == 3
