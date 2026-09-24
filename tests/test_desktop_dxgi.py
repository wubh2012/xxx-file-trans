"""desktop 源 DXGI 采集适配器（issue #31 B2）。

不触碰真实屏幕/真实 dxcam：_dxgi_capture 经 sys.modules 注入假 dxcam
（create 返回按脚本回放的假相机），验证三件事——region dict → ltrb 换算、
无更新（grab None）时重发上一帧的 mss 等价语义、首帧未到时等待不产出。
iter_desktop(backend="dxgi") 与 StableFrameGate 的衔接复用既有 desktop
闸门用例的语义（此处仅走通一条端到通路）。
"""

import gzip
import sys
import zlib
from itertools import islice
from pathlib import Path

import cv2
import numpy as np
import pytest

import fixture_encoder
from receiver.sources.desktop import iter_desktop, _dxgi_capture


DXGI_LOST = object()


class FakeCamera:
    """按脚本回放的假相机：脚本元素为帧（(H,W,1) 灰度）、None（无更新）或
    DXGI_LOST 哨兵（grab 抛异常，模拟系统过渡致 duplication 失效）。"""

    def __init__(self, script: list):
        self.script = list(script)
        self.pos = 0
        self.released = False

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
