"""video 取帧源（issue #10，预录视频解帧）。

测试用代码生成小体积合成视频（cv2.VideoWriter 写夹具 PNG 帧），不经
真实屏幕与真实录制。编码器选 FFV1（无损）：编解码往返逐像素一致，
夹具聚焦逐帧读取与还原通路；噪点容错由噪点注入用例覆盖。
经 CLI 同一条 receive 通路断言还原结果（spec「desktop/video 通道：
与 images 共用缝 B」）。
"""

import argparse
import gzip
import zlib
from pathlib import Path

import cv2
import numpy as np
import pytest

import fixture_encoder
from receiver.cli import main, receive_frames
from receiver.pipeline import FrameRejected, decode_frame
from receiver.sources.video import iter_video
from receiver.sources.stable import StableFrameGate


def _read_gray(p: Path) -> np.ndarray:
    img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
    assert img is not None
    return img


def _write_video(path: Path, frames: list[np.ndarray], fps: float = 30.0) -> None:
    """合成预录视频：帧序列按录制帧率写入（FFV1 无损，往返逐像素一致）。"""
    h, w = frames[0].shape
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"FFV1"), fps, (w, h),
                         isColor=False)
    assert vw.isOpened(), f"VideoWriter 打开失败：{path}"
    for img in frames:
        vw.write(img)
    vw.release()


def _export_frames(tmp_path: Path, src: bytes, filename: str) -> list[Path]:
    comp = gzip.compress(src)
    return fixture_encoder.export_frames(
        comp, zlib.crc32(src) & 0xFFFFFFFF, tmp_path / "frames",
        filename=filename, plain_size=len(src),
        cols=48, rows=48, bit=4, pad=3,
    )


# ---------- iter_video（真实 VideoCapture 解码）----------

def test_iter_video_emits_each_frame_once(tmp_path):
    """录屏中的重复画面也逐帧放行，名称按读取顺序连续编号。"""
    src = b"video roundtrip"
    paths = _export_frames(tmp_path, src, "vr.bin")
    imgs = [_read_gray(p) for p in paths]
    video = tmp_path / "recording.avi"
    _write_video(video, [im for im in imgs for _ in range(2)], fps=30.0)  # 每个传输帧连续重复 2 次

    names = [name for name, _ in iter_video(video)]
    assert names == [f"video-{i:06d}" for i in range(1, len(imgs) * 2 + 1)]


def test_iter_video_preserves_repeated_frames(tmp_path):
    """重复捕获全部保留，让下游按帧号去重，避免跳过有效的单次画面。"""
    src = b"change detection"
    paths = _export_frames(tmp_path, src, "cd.bin")
    imgs = [_read_gray(p) for p in paths]
    video = tmp_path / "recording.avi"
    _write_video(video, [im for im in imgs for _ in range(4)], fps=30.0)

    names = [name for name, _ in iter_video(video)]
    assert len(names) == len(paths) * 4


def test_iter_video_preserves_single_trailing_frame(tmp_path):
    """末帧只出现一次时仍正常读取，不需要稳定等待或流末补发。"""
    src = b"trailing frame"
    paths = _export_frames(tmp_path, src, "tf.bin")
    imgs = [_read_gray(p) for p in paths]
    video = tmp_path / "recording.avi"
    # 前面的传输帧各重复 2 次，最后一个传输帧只出现 1 次（截尾）
    _write_video(video, [im for im in imgs[:-1] for _ in range(2)] + [imgs[-1]],
                 fps=30.0)

    names = [name for name, _ in iter_video(video)]
    assert names == [f"video-{i:06d}" for i in range(1, len(imgs) * 2)]


# ---------- CLI 分派 ----------

def test_cli_video_without_video_flag_exits_2():
    with pytest.raises(SystemExit) as e:
        main(["receive", "--source", "video"])
    assert e.value.code == 2


def test_cli_video_dispatch_passes_video_path(monkeypatch):
    """分派接线：--video 路径传入 iter_video；Ctrl+C → 退出码 1。"""
    import receiver.cli as cli

    seen = {}

    def fake_iter_video(video, *, tape=False):
        seen["video"] = video
        yield "video-000001", np.zeros((10, 10), np.uint8)  # 首帧正常进入循环
        raise KeyboardInterrupt  # 模拟接收中 Ctrl+C（真实生成器异常在迭代中发生）

    monkeypatch.setattr(cli, "iter_video", fake_iter_video)
    rc = main(["receive", "--source", "video", "--video", "rec.avi"])
    assert rc == 1
    assert seen["video"] == Path("rec.avi")


# ---------- 缝 B 集成：video 通路 → 还原（含噪点注入）----------

def _recording_with_noise(paths: list[Path], rng) -> list[np.ndarray]:
    """预录视频夹具（含噪点）：

    1. 每个传输帧前插一次随机画面（录制时的过渡/干扰画面，由流水线拒绝）；
    2. 每个传输帧连续重复两次（录制帧率高于发送帧率）→ 按帧号去重；
    3. 插入坏帧变体的两个副本，两帧均读取并由 CRC 拒绝，
       不得污染已收好帧。
    """
    frames: list[np.ndarray] = []
    emitted = 0
    bad_drawn = False
    for p in paths:
        good = _read_gray(p)
        frames.append(rng.integers(0, 256, size=good.shape, dtype=np.uint8))
        emitted += 1
        if emitted == 4 and not bad_drawn:  # paths[0] 是元数据帧，paths[3] 是数据帧 2
            bad = good.copy()
            bad[40:48, 40:48] = 255 - bad[40:48, 40:48]
            frames += [bad, bad]
            bad_drawn = True
        frames += [good, good]
    return frames


def test_video_channel_restore_with_noise(tmp_path):
    """逐帧读取含过渡噪声和重复坏帧的录屏，协议校验拒绝坏帧，最终字节一致。"""
    src = np.random.default_rng(42).bytes(1200)
    paths = _export_frames(tmp_path, src, "noisy.bin")
    rng = np.random.default_rng(42)
    video = tmp_path / "recording.avi"
    _write_video(video, _recording_with_noise(paths, rng), fps=30.0)

    out = tmp_path / "output"
    args = argparse.Namespace(source="video", dir=None,
                              video=video, region=None, out=out)

    passed: list[tuple[str, np.ndarray]] = []  # 全部读取帧（receive 主循环的输入）

    def record_passed():
        for name, img in iter_video(video):
            passed.append((name, img))
            yield name, img

    r = receive_frames(args, record_passed())

    assert r == 0
    files = list(out.iterdir())
    assert len(files) == 1 and files[0].name == "noisy.bin"
    assert files[0].read_bytes() == src

    # 两个坏帧副本均被 CRC 拒绝，过渡噪声也由流水线拒绝
    reasons = []
    for _, img in passed:
        try:
            decode_frame(img)
        except FrameRejected as e:
            reasons.append(e.reason)
    assert reasons.count("crc") == 2, f"两个坏帧副本应均被 CRC 拒绝，实际 {reasons}"


# ---------- 输入错误 ----------

def test_iter_video_unopenable_file_raises(tmp_path):
    """打不开的视频（不存在 / 编码器不支持）显式报错，不静默产出零帧。"""
    with pytest.raises(ValueError):
        list(iter_video(tmp_path / "missing.avi"))


def test_cli_video_unopenable_file_exits_2(tmp_path):
    """CLI：视频打不开 → 明确报错，退出码 2，而非以「未收齐」退出码 1 收场。"""
    rc = main(["receive", "--source", "video", "--video", str(tmp_path / "no.avi")])
    assert rc == 2


# ---------- 片模式（issue #45）：旁路稳定闸门逐帧直读 ----------

def test_video_single_frame_per_payload_restores_without_tape_flag(tmp_path):
    """不勾选片模式也必须保留每个传输帧，不能把有限视频当作实时采集流。"""
    src = np.random.default_rng(20261002).bytes(1200)
    paths = _export_frames(tmp_path, src, "single.bin")
    video = tmp_path / "single.avi"
    _write_video(video, [_read_gray(p) for p in paths])
    out = tmp_path / "output"
    args = argparse.Namespace(source="video", out=out)

    assert receive_frames(args, iter_video(video, tape=False)) == 0
    assert (out / "single.bin").read_bytes() == src


def test_iter_video_tape_mode_emits_every_decoded_frame(tmp_path):
    """旧 tape 开关两种取值均逐帧放行；原稳定闸门无法处理单次画面。"""
    src = b"tape mode"
    paths = _export_frames(tmp_path, src, "tp.bin")
    imgs = [_read_gray(p) for p in paths]
    video = tmp_path / "tape.avi"
    _write_video(video, imgs, fps=30.0)  # 每传输帧只出现 1 次（无重复捕获）

    names = [name for name, _ in iter_video(video, tape=True)]
    assert names == [f"video-{i:06d}" for i in range(1, len(paths) + 1)]

    default_names = [name for name, _ in iter_video(video)]
    assert default_names == names
    gate = StableFrameGate()
    gated = [gate.feed(img, now=i / 30) for i, img in enumerate(imgs)]
    assert sum(img is not None for img in gated) == 0


def test_cli_video_tape_flag_passed_through(monkeypatch):
    """CLI 分派：--tape 透传 iter_video 的 tape 参数（保留旧参数兼容）。"""
    import receiver.cli as cli

    seen = {}

    def fake_iter_video(video, *, tape=False):
        seen["tape"] = tape
        yield "video-000001", np.zeros((10, 10), np.uint8)
        raise KeyboardInterrupt  # 首帧进入循环后即停，收尾语义不在本用例

    monkeypatch.setattr(cli, "iter_video", fake_iter_video)
    assert main(["receive", "--source", "video", "--video", "rec.avi"]) == 1
    assert seen["tape"] is False
    assert main(["receive", "--source", "video", "--video", "rec.avi", "--tape"]) == 1
    assert seen["tape"] is True
