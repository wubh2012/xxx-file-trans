"""video 取帧源（issue #10，预录视频解帧）。

测试用代码生成小体积合成视频（cv2.VideoWriter 写夹具 PNG 帧），不经
真实屏幕与真实录制。编码器选 FFV1（无损）：编解码往返逐像素一致，
夹具聚焦稳定闸门过滤与还原通路；噪点容错由噪点注入用例覆盖。
经 CLI 同一条 receive 通路断言还原结果（spec「desktop/video 通道：
与 images 共用缝 B」）。
"""

import argparse
import gzip
import os
import zlib
from pathlib import Path

import cv2
import numpy as np
import pytest

import fixture_encoder
from receiver.cli import main, receive_frames
from receiver.pipeline import FrameRejected, decode_frame
from receiver.sources.video import iter_video


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
    """录制帧率高于发送帧率：每个传输帧在视频里重复多次，稳定闸门
    （与 desktop 共用）过滤后各放行一次，名称按放行顺序连续编号。"""
    src = b"video roundtrip"
    paths = _export_frames(tmp_path, src, "vr.bin")
    imgs = [_read_gray(p) for p in paths]
    video = tmp_path / "recording.avi"
    _write_video(video, [im for im in imgs for _ in range(2)], fps=30.0)  # 每个传输帧连续重复 2 次

    names = [name for name, _ in iter_video(video)]
    assert names == [f"video-{i:06d}" for i in range(1, len(paths) + 1)]


def test_iter_video_change_detection(tmp_path):
    """重复捕获（录制帧率远高于发送帧率 / 循环重播片段）被变化检测
    吞掉，不重复放行。"""
    src = b"change detection"
    paths = _export_frames(tmp_path, src, "cd.bin")
    imgs = [_read_gray(p) for p in paths]
    video = tmp_path / "recording.avi"
    _write_video(video, [im for im in imgs for _ in range(4)], fps=30.0)

    names = [name for name, _ in iter_video(video)]
    assert len(names) == len(paths)


def test_iter_video_flushes_last_frame_at_end_of_stream(tmp_path):
    """录制恰好在最后一个传输帧只出现一次时截尾：流末 flush 放行滞留
    候选，末帧不静默丢失。"""
    src = b"trailing frame"
    paths = _export_frames(tmp_path, src, "tf.bin")
    imgs = [_read_gray(p) for p in paths]
    video = tmp_path / "recording.avi"
    # 前面的传输帧各重复 2 次，最后一个传输帧只出现 1 次（截尾）
    _write_video(video, [im for im in imgs[:-1] for _ in range(2)] + [imgs[-1]],
                 fps=30.0)

    names = [name for name, _ in iter_video(video)]
    assert names == [f"video-{i:06d}" for i in range(1, len(paths) + 1)]


# ---------- CLI 分派 ----------

def test_cli_video_without_video_flag_exits_2():
    with pytest.raises(SystemExit) as e:
        main(["receive", "--source", "video"])
    assert e.value.code == 2


def test_cli_video_dispatch_passes_video_path(monkeypatch):
    """分派接线：--video 路径传入 iter_video；Ctrl+C → 退出码 1。"""
    import receiver.cli as cli

    seen = {}

    def fake_iter_video(video):
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

    1. 每个传输帧前插一次随机画面（录制时的过渡/干扰画面，不稳定 →
       永不放行）；
    2. 每个传输帧连续重复两次（录制帧率高于发送帧率）→ 稳定放行；
    3. 第 2 个数据帧之后插入它的坏帧变体连续两帧（翻转 8×8 = 4 个
       方块，损坏面积须超闸门伪影容差，否则被变化检测吞掉而非放行）
       —— 闸门放行、CRC 整帧拒绝，不得污染已收好帧。
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
    """video 通路集成：含噪点预录视频 → 稳定闸门 → CLI 还原，字节一致。

    固化「闸门放行 → CRC 拒绝」路径（issue #16）：坏帧变体损坏面积超
    闸门伪影容差被放行，CRC 整帧拒绝——断言闸门放行帧中恰有 1 帧因
    CRC 被拒。若坏帧构造回归到低于容差（如 4×4），会被变化检测吞掉、
    放行帧全部解码成功，此断言随即失败。"""
    src = os.urandom(1200)
    paths = _export_frames(tmp_path, src, "noisy.bin")
    rng = np.random.default_rng(42)
    video = tmp_path / "recording.avi"
    _write_video(video, _recording_with_noise(paths, rng), fps=30.0)

    out = tmp_path / "output"
    args = argparse.Namespace(source="video", dir=None,
                              video=video, region=None, out=out)

    passed: list[tuple[str, np.ndarray]] = []  # 闸门放行帧（receive 主循环的输入）

    def record_passed():
        for name, img in iter_video(video):
            passed.append((name, img))
            yield name, img

    r = receive_frames(args, record_passed())

    assert r == 0
    files = list(out.iterdir())
    assert len(files) == 1 and files[0].name == "noisy.bin"
    assert files[0].read_bytes() == src

    # 恰有 1 帧（坏帧变体）被 CRC 整帧拒绝，好帧全部解码成功
    reasons = []
    for _, img in passed:
        try:
            decode_frame(img)
        except FrameRejected as e:
            reasons.append(e.reason)
    assert reasons == ["crc"], f"闸门放行帧应恰有 1 帧 CRC 拒绝，实际 {reasons}"


# ---------- 输入错误 ----------

def test_iter_video_unopenable_file_raises(tmp_path):
    """打不开的视频（不存在 / 编码器不支持）显式报错，不静默产出零帧。"""
    with pytest.raises(ValueError):
        list(iter_video(tmp_path / "missing.avi"))


def test_cli_video_unopenable_file_exits_2(tmp_path):
    """CLI：视频打不开 → 明确报错，退出码 2，而非以「未收齐」退出码 1 收场。"""
    rc = main(["receive", "--source", "video", "--video", str(tmp_path / "no.avi")])
    assert rc == 2
