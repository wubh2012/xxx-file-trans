"""desktop 源 DXGI 采集适配器（issue #31 B2）。

不触碰真实屏幕/真实 dxcam：_dxgi_capture 经 sys.modules 注入假 dxcam
（create 返回按脚本回放的假相机），验证三件事——region dict → ltrb 换算、
无更新（grab None）时重发上一帧的 mss 等价语义、首帧未到时等待不产出。
iter_desktop(backend="dxgi") 与 StableFrameGate 的衔接复用既有 desktop
闸门用例的语义（此处仅走通一条端到通路）。
"""

import gzip
import sys
import time
import zlib
from itertools import islice
from pathlib import Path

import cv2
import numpy as np
import pytest

import fixture_encoder
from receiver.sources.desktop import (DXGIRecoveryError, iter_desktop,
                                      _dxgi_capture)


DXGI_LOST = object()


class FakeCamera:
    """按脚本回放的假相机：脚本元素为帧（(H,W,1) 灰度）、None（无更新）或
    DXGI_LOST 哨兵（grab 抛异常，模拟系统过渡致 duplication 失效）。"""

    def __init__(self, script: list):
        self.script = list(script)
        self.pos = 0
        self.released = False
        self.started = False
        self.start_calls = 0

    def grab(self, new_frame_only: bool = True):
        if self.pos >= len(self.script):
            return None  # 脚本耗尽视为静屏（与真实 dxcam 静屏行为一致）
        item = self.script[self.pos]
        self.pos += 1
        if item is DXGI_LOST:
            raise RuntimeError("DXGI error 0x887A0026")
        return item

    def release(self):
        self.released = True

    def start(self, **kwargs):
        self.start_calls += 1
        self.started = True

    @property
    def is_capturing(self):
        return self.started

    def stop(self):
        self.started = False


class FakeDXCam:
    """假 dxcam 模块：记录 create 参数与次数，按脚本队列回放（队列耗尽后
    复用最后一个脚本——重建相机常为静屏语义）。"""

    def __init__(self, scripts: list):
        self.scripts = [list(s) for s in scripts]
        self.script: list = []
        self.created_with: dict | None = None
        self.create_count = 0
        self.camera: FakeCamera | None = None

    def create(self, output_color: str = "RGB", region=None):
        self.created_with = {"output_color": output_color, "region": region}
        self.create_count += 1
        if self.scripts:
            self.script = self.scripts.pop(0)
        self.camera = FakeCamera(self.script)
        return self.camera


DXGI_LOST = object()


def _gray3d(img: np.ndarray) -> np.ndarray:
    """(H,W) 灰度 → dxcam GRAY 输出形状 (H,W,1)。"""
    return img[:, :, None]


@pytest.fixture
def fake_dxcam(monkeypatch):
    def _install(script: list, rebuild_script: list | None = None) -> FakeDXCam:
        scripts = [script] + ([rebuild_script] if rebuild_script is not None else [])
        fake = FakeDXCam(scripts)
        monkeypatch.setitem(sys.modules, "dxcam", fake)
        return fake
    return _install


def test_region_dict_to_ltrb(tmp_path, fake_dxcam):
    """region dict {left,top,width,height} → DXcam ltrb 元组。"""
    img = np.zeros((8, 8, 1), dtype=np.uint8)
    fake = fake_dxcam([_gray3d(img)])
    gen = _dxgi_capture({"left": 10, "top": 20, "width": 300, "height": 200})
    next(gen)
    gen.close()
    assert fake.created_with == {"output_color": "GRAY", "region": (10, 20, 310, 220)}


def test_dxgi_uses_threaded_capture_mode(fake_dxcam):
    """真实 dxcam 使用 start + 环形缓冲区，主循环不直接阻塞在 DXGI grab。"""
    img = np.zeros((4, 4, 1), dtype=np.uint8)
    fake = fake_dxcam([_gray3d(img)])
    gen = _dxgi_capture(None)
    next(gen)
    gen.close()
    assert fake.camera is not None and fake.camera.start_calls == 1


def test_threaded_capture_does_not_stabilize_short_lived_buffer_snapshot(monkeypatch):
    """线程缓冲区读取过快时，同一过渡快照不能被立即读两次并放行。"""
    clock = [0.0]
    rng = np.random.default_rng(42)
    a, partial, b = [rng.integers(0, 256, (16, 16), dtype=np.uint8)
                     for _ in range(3)]

    class BufferedCamera(FakeCamera):
        def grab(self, new_frame_only=True):
            clock[0] += 0.0003  # 非阻塞环形缓冲区读取
            img = a if clock[0] < 0.030 else partial if clock[0] < 0.034 else b
            return _gray3d(img)

        @property
        def latest_frame_ticks(self):
            return int(clock[0] * 120)

    camera = BufferedCamera([])

    class BufferedDXCam:
        @staticmethod
        def create(**kwargs):
            return camera

    monkeypatch.setitem(sys.modules, "dxcam", BufferedDXCam())
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    capture = _dxgi_capture(None)
    try:
        emitted = [img for _, img in iter_desktop(capture=islice(capture, 150))]
    finally:
        capture.close()
    assert len(emitted) == 2, "4ms 过渡快照不应被快速重复读取判为稳定画面"
    assert np.array_equal(emitted[0], a)
    assert np.array_equal(emitted[1], b)


def test_no_update_repeats_last_frame(tmp_path, fake_dxcam):
    """静屏（grab None）重发上一帧——稳定闸门两帧一致判定依赖的重复捕获语义。"""
    a = np.full((8, 8, 1), 10, dtype=np.uint8)
    b = np.full((8, 8, 1), 240, dtype=np.uint8)
    fake_dxcam([_gray3d(a), None, None, _gray3d(b), None])
    gen = _dxgi_capture(None)
    out = [next(gen), next(gen), next(gen), next(gen), next(gen)]
    gen.close()
    assert all(o.shape == (8, 8) for o in out)  # (H,W,1) → (H,W)
    assert np.array_equal(out[0], out[1]) and np.array_equal(out[1], out[2])
    assert not np.array_equal(out[2], out[3])   # 更新帧
    assert np.array_equal(out[3], out[4])       # 再次静屏 → 重发新帧


def test_waits_for_first_update(fake_dxcam):
    """首帧未到（静屏启动）：不产出帧，直到首个屏幕更新。"""
    img = np.zeros((4, 4, 1), dtype=np.uint8)
    fake_dxcam([None, None, _gray3d(img)])
    gen = _dxgi_capture(None)
    out = next(gen)
    gen.close()
    assert out.shape == (4, 4)


def test_generator_close_releases_camera(fake_dxcam):
    """生成器关闭 → 相机释放（随迭代器关闭释放采集资源）。"""
    fake = fake_dxcam([np.zeros((4, 4, 1), dtype=np.uint8)])
    gen = _dxgi_capture(None)
    next(gen)
    gen.close()
    assert fake.camera is not None and fake.camera.released


def test_grab_exception_retry_then_rebuild(fake_dxcam, monkeypatch):
    """grab 抛异常（系统过渡 ACCESS_LOST）→ 先重试 4 拍（重发上一帧），
    连续 5 次失败才释放并重建相机；恢复期继续重发上一帧——单次过渡
    不终止接收（issue #31）。"""
    monkeypatch.setattr("receiver.sources.desktop.time.sleep", lambda _s: None)
    a = np.full((8, 8, 1), 10, dtype=np.uint8)
    fake = fake_dxcam([_gray3d(a)] + [DXGI_LOST] * 5, rebuild_script=[])
    gen = _dxgi_capture(None)
    out = [next(gen) for _ in range(6)]
    gen.close()
    assert all(np.array_equal(o, a[:, :, 0]) for o in out)  # 重试/恢复期全为重发
    assert fake.create_count == 2           # 相机被重建一次


def test_grab_transient_exception_no_rebuild(fake_dxcam, monkeypatch):
    """零星 grab 异常（≤4 次）在 dxcam 自带恢复内消化，不重建相机。"""
    monkeypatch.setattr("receiver.sources.desktop.time.sleep", lambda _s: None)
    a = np.full((8, 8, 1), 10, dtype=np.uint8)
    b = np.full((8, 8, 1), 240, dtype=np.uint8)
    fake = fake_dxcam([_gray3d(a), DXGI_LOST, DXGI_LOST, _gray3d(b)])
    gen = _dxgi_capture(None)
    out = [next(gen), next(gen), next(gen), next(gen)]
    gen.close()
    assert np.array_equal(out[0], a[:, :, 0])
    assert np.array_equal(out[1], out[2])   # 异常拍重发上一帧
    assert np.array_equal(out[3], b[:, :, 0])  # 恢复后新帧
    assert fake.create_count == 1           # 相机未被重建


def test_persistent_access_lost_fails_fast(fake_dxcam, monkeypatch):
    """持续 ACCESS_LOST 不得无限重试：达到恢复周期上限后明确失败，
    上层才能切换 mss 或向用户报告，而不是静默卡死。"""
    monkeypatch.setattr("receiver.sources.desktop.time.sleep", lambda _s: None)
    monkeypatch.setattr("receiver.sources.desktop.DXGI_MAX_RECOVERY_CYCLES", 2)
    a = np.full((8, 8, 1), 10, dtype=np.uint8)
    fake_dxcam([_gray3d(a)] + [DXGI_LOST] * 5,
               rebuild_script=[DXGI_LOST] * 5)
    gen = _dxgi_capture(None)
    assert np.array_equal(next(gen), a[:, :, 0])
    with pytest.raises(DXGIRecoveryError):
        for _ in range(12):
            next(gen)
    gen.close()


def test_iter_desktop_falls_back_to_mss_after_persistent_access_lost(
        fake_dxcam, monkeypatch):
    """DXGI 持续失效时 desktop 源自动切 mss，任务继续产出画面。"""
    monkeypatch.setattr("receiver.sources.desktop.time.sleep", lambda _s: None)
    monkeypatch.setattr("receiver.sources.desktop.DXGI_MAX_RECOVERY_CYCLES", 1)
    a = np.full((8, 8, 1), 10, dtype=np.uint8)
    fake_dxcam([_gray3d(a)] + [DXGI_LOST] * 5)
    mss_frames = iter([a[:, :, 0], a[:, :, 0]])
    monkeypatch.setattr("receiver.sources.desktop._mss_capture",
                        lambda _region: mss_frames)
    gen = iter_desktop(backend="dxgi")
    name, frame = next(gen)
    gen.close()
    assert name == "desktop-000001"
    assert np.array_equal(frame, a[:, :, 0])


def test_empty_dxgi_frames_fail_fast(monkeypatch):
    """dxcam 不抛异常但连续返回 None 时同样必须触发恢复出口。"""
    monkeypatch.setattr("receiver.sources.desktop.DXGI_EMPTY_TIMEOUT_S", 0.0)
    monkeypatch.setattr("receiver.sources.desktop.time.sleep", lambda _s: None)
    class EmptyCamera:
        def grab(self, new_frame_only=True):
            return None
        def release(self):
            pass
    class EmptyDXCam:
        def create(self, **kwargs):
            return EmptyCamera()
    monkeypatch.setitem(__import__("sys").modules, "dxcam", EmptyDXCam())
    gen = _dxgi_capture(None)
    with pytest.raises(DXGIRecoveryError):
        next(gen)
    gen.close()


def test_dxgi_shutdown_is_bounded(monkeypatch):
    """dxcam stop 卡住时，清理不能阻塞接收主循环。"""
    from receiver.sources.desktop import _release_dxgi_camera

    class StuckCamera:
        released = False

        def stop(self):
            time.sleep(0.2)

        def release(self):
            self.released = True

    monkeypatch.setattr("receiver.sources.desktop.DXGI_SHUTDOWN_TIMEOUT_S", 0.01)
    camera = StuckCamera()
    started = time.perf_counter()
    _release_dxgi_camera(camera)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.1
    assert not camera.released


def test_stopped_threaded_capture_fails_even_with_stale_frame(fake_dxcam):
    """采集线程停止但环形缓冲仍有旧帧时，也必须立即走恢复出口。"""
    a = np.full((4, 4, 1), 10, dtype=np.uint8)
    fake = fake_dxcam([_gray3d(a)])
    gen = _dxgi_capture(None)
    # 假相机首帧可读后模拟内部线程已退出；旧帧仍可能被 grab 返回。
    next(gen)
    assert fake.camera is not None
    fake.camera.started = False
    with pytest.raises(DXGIRecoveryError):
        next(gen)
    gen.close()


def test_stalled_latest_frame_ticks_fail_fast(monkeypatch):
    """DXGI 返回旧帧但时间戳不再推进时必须触发回退。"""
    import sys

    monkeypatch.setattr("receiver.sources.desktop.DXGI_FRAME_STALL_TIMEOUT_S", 0.0)
    class StaleCamera:
        is_capturing = True
        latest_frame_ticks = 1
        def start(self, **kwargs):
            pass
        def grab(self, **kwargs):
            return np.zeros((4, 4, 1), dtype=np.uint8)
        def stop(self):
            pass
        def release(self):
            pass
    class StaleDXCam:
        def create(self, **kwargs):
            return StaleCamera()
    monkeypatch.setitem(sys.modules, "dxcam", StaleDXCam())
    gen = _dxgi_capture(None)
    next(gen)
    with pytest.raises(DXGIRecoveryError):
        next(gen)
    gen.close()


def test_create_exception_retries(fake_dxcam, monkeypatch):
    """create 抛异常（过渡期重建失效）→ 不终止采集，重试直至成功。"""
    monkeypatch.setattr("receiver.sources.desktop.time.sleep", lambda _s: None)
    a = np.full((8, 8, 1), 10, dtype=np.uint8)

    class CreateFlakyDXCam(FakeDXCam):
        def create(self, output_color: str = "RGB", region=None):
            super().create(output_color=output_color, region=region)
            if self.create_count == 1:
                raise RuntimeError("DXGI error 0x887A0026")  # 首次重建仍失效
            return self.camera  # 第二次成功

    fake = CreateFlakyDXCam([[], [_gray3d(a)]])
    monkeypatch.setitem(sys.modules, "dxcam", fake)
    gen = _dxgi_capture(None)
    out = next(gen)
    gen.close()
    assert np.array_equal(out, a[:, :, 0])
    assert fake.create_count == 2


def test_iter_desktop_dxgi_backend_end_to_end(tmp_path, fake_dxcam):
    """backend="dxgi" 全通路：假相机脚本（重复捕获节奏）→ 闸门放行 → 命名。"""
    src = b"dxgi roundtrip"
    comp = gzip.compress(src)
    paths = fixture_encoder.export_frames(
        comp, zlib.crc32(src) & 0xFFFFFFFF, tmp_path / "frames",
        filename="dx.bin", plain_size=len(src),
        cols=48, rows=48, bit=4, pad=3,
    )
    script: list = []
    for p in paths:  # 每个传输帧：新画面 + 一次静屏重复（两帧一致判定所需）
        img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
        script += [_gray3d(img), None]
    fake = fake_dxcam(script)
    # 真实 dxgi 采集是无限流（脚本耗尽后假相机持续静屏重发），按放行数截取
    gen = iter_desktop(backend="dxgi")
    names = [name for name, _ in islice(gen, len(paths))]
    gen.close()
    assert names == [f"desktop-{i:06d}" for i in range(1, len(paths) + 1)]
    assert fake.camera is not None and fake.camera.released  # 关闭释放采集资源


def test_iter_desktop_close_releases_camera(fake_dxcam):
    """iter_desktop 关闭 → 内层 dxgi 采集生成器被显式 close → 相机释放
    （滞留消费方清理不依赖 GC 终结时机，bench 超时收尾依赖此语义）。"""
    fake = fake_dxcam([np.zeros((4, 4, 1), dtype=np.uint8)])
    gen = iter_desktop(backend="dxgi")
    next(gen)
    gen.close()
    assert fake.camera is not None and fake.camera.released


def _install_finalizing_threading(monkeypatch):
    """把 desktop 模块的 threading 换成「解释器关闭期」替身：线程创建被禁
    （RuntimeError，3.13+ 为其子类 PythonFinalizationError），Event 仍可用。"""
    import threading

    import receiver.sources.desktop as desktop_module

    class FinalizingThreading:
        Event = threading.Event

        @staticmethod
        def Thread(*args, **kwargs):
            raise RuntimeError("can't create new thread at interpreter shutdown")

    monkeypatch.setattr(desktop_module, "threading", FinalizingThreading)
    return desktop_module


def test_release_camera_falls_back_to_sync_stop_at_finalization(monkeypatch):
    """解释器关闭期线程创建被禁 → 回退同步 stop + release，清理路径不得
    二次崩溃把退出码从业务失败搅成解释器错误（issue #43）。"""
    from receiver.sources.desktop import _release_dxgi_camera

    class Camera:
        released = False
        stopped = False

        def stop(self):
            self.stopped = True

        def release(self):
            self.released = True

    _install_finalizing_threading(monkeypatch)
    camera = Camera()
    _release_dxgi_camera(camera)
    assert camera.stopped and camera.released


def test_release_camera_sync_stop_failure_still_releases(monkeypatch):
    """同步回退路径上 stop 自身抛错（dxcam 内部已崩）：同样吞掉，
    release 照常执行，不覆盖原始业务错误。"""
    from receiver.sources.desktop import _release_dxgi_camera

    class Camera:
        released = False

        def stop(self):
            raise RuntimeError("capture thread already dead")

        def release(self):
            self.released = True

    _install_finalizing_threading(monkeypatch)
    camera = Camera()
    _release_dxgi_camera(camera)
    assert camera.released
