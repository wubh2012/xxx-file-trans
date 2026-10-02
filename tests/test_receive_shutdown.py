import queue
import threading
import time

import numpy as np

from receiver.gui_core import ReceiveJob, ProgressModel
from receiver.run import PrefetchedFrames, ReceiveResult


def test_static_desktop_capture_stops_and_releases_on_owner_thread():
    from receiver.sources.desktop import iter_desktop
    owners = []
    closed = []

    def capture():
        owners.append(threading.get_ident())
        try:
            while True:
                yield np.zeros((16, 16), dtype=np.uint8)
        finally:
            closed.append(threading.get_ident())

    stream = PrefetchedFrames(iter_desktop(capture=capture()))
    next(iter(stream))
    stream.close()
    assert not stream._thread.is_alive()
    assert closed == owners
    assert stream.cleanup_error is None


def test_prefetch_closes_source_on_capture_thread():
    owners = []
    closed = []

    def frames():
        owners.append(threading.get_ident())
        try:
            while True:
                yield "frame", None
        finally:
            closed.append(threading.get_ident())

    stream = PrefetchedFrames(frames())
    next(iter(stream))
    stream.close()
    assert closed == owners


def test_stop_while_capture_has_no_frame(tmp_path, monkeypatch):
    from receiver import paths
    monkeypatch.setattr(paths, "PROGRESS_DIR", tmp_path / "progress")
    entered = threading.Event()
    release = threading.Event()

    def frames():
        entered.set()
        release.wait(5)
        yield "frame", None

    events = queue.Queue()
    job = ReceiveJob(frames, tmp_path / "out", events, prefetch=True)
    job.start()
    assert entered.wait(1)
    job.stop()
    try:
        deadline = time.monotonic() + 1.5
        while True:
            event = events.get(timeout=max(0.01, deadline - time.monotonic()))
            if event[0] == "done":
                assert event[1].stopped
                break
    finally:
        release.set()


def test_cleanup_error_still_delivers_done(tmp_path, monkeypatch):
    import receiver.gui_core as gc

    class Frames:
        def close(self):
            raise RuntimeError("capture cleanup failed")

    monkeypatch.setattr(gc, "run_receive", lambda *a, **kw: ReceiveResult(code=0))
    events = queue.Queue()
    ReceiveJob(Frames, tmp_path, events)._run()
    event = events.get_nowait()
    assert event[0] == "done" and event[1].code == 0
    assert "capture cleanup failed" in event[1].warnings[0]


def test_progress_clock_freezes_on_stop():
    now = [10.0]
    model = ProgressModel(clock=lambda: now[0])
    model.on_event(("decoded", True, 1))
    now[0] = 20.0
    model.stop()
    now[0] = 100.0
    assert model.elapsed == 10.0
