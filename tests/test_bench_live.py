"""真实屏幕基准的资源生命周期回归测试。"""

from benchmark import run_bench_live


def test_calibration_capture_is_closed_before_receive(monkeypatch):
    """校准结束必须显式 close 采集生成器，避免 DXGI 单例被旧线程占用。"""
    class FakeGenerator:
        closed = False

        def close(self):
            self.closed = True

        def __iter__(self):
            return iter(())

    fake = FakeGenerator()
    monkeypatch.setattr(run_bench_live, "iter_desktop", lambda **_: fake)
    monkeypatch.setattr(run_bench_live, "run_calibration", lambda _frames: {"ok": True})
    assert run_bench_live.calibrate_capture({}, "dxgi", 0.0) == {"ok": True}
    assert fake.closed


def test_alignment_selfcheck_falls_back_to_mss(monkeypatch):
    """区域自检遇到 DXGI ACCESS_LOST 时也必须切换 mss。"""
    import numpy as np

    seen = []

    class Failing:
        def __iter__(self):
            return self
        def __next__(self):
            raise run_bench_live.DXGIRecoveryError("lost")
        def close(self):
            pass

    class Good:
        def __iter__(self):
            return self
        def __next__(self):
            return np.zeros((4, 4), dtype=np.uint8)
        def close(self):
            pass

    def fake_capture(region, backend):
        seen.append(backend)
        return Failing() if backend == "dxgi" else Good()

    monkeypatch.setattr(run_bench_live, "capture_generator", fake_capture)
    monkeypatch.setattr(run_bench_live, "decode_frame", lambda _img: None)
    assert run_bench_live.alignment_selfcheck({}, backend="dxgi", tries=2)
    assert seen == ["dxgi", "mss"]
