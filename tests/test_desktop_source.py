"""desktop 取帧源（issue #9，mss 抓屏）。

测试不触碰真实屏幕：mss 抓屏是薄适配器，画面经 StableFrameGate 过滤
后产出统一 (名称, 灰度图) 迭代器。测试从 capture 注入缝喂入夹具图像
序列（fixture_encoder 渲染），经 CLI 同一条 receive 通路断言还原结果
（spec「desktop 夹具为预录图像序列，含噪点注入用例」）。真实屏幕采集
的人工验收（1 MB / 1 分钟）在 M2 出口执行。
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
from receiver.sources.desktop import iter_desktop, parse_region


# ---------- parse_region ----------

def test_parse_region_ok():
    assert parse_region("10,20,300,200") == {
        "left": 10, "top": 20, "width": 300, "height": 200,
    }


@pytest.mark.parametrize("bad", ["1,2,3", "a,b,c,d", "1,2,3,0", "1,2,3,-4", "1,2,3,4,5", ""])
def test_parse_region_rejects(bad):
    with pytest.raises(ValueError):
        parse_region(bad)


# ---------- iter_desktop（capture 注入缝）----------

def _read_gray(p: Path) -> np.ndarray:
    img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
    assert img is not None
    return img


def _captures(frames_png: list[Path], repeats: int = 2):
    """抓屏夹具：每个传输帧捕获 repeats 次（稳定两帧判定所需）。"""
    for p in frames_png:
        for _ in range(repeats):
            yield _read_gray(p)


def test_iter_desktop_emits_each_frame_once(tmp_path):
    """无限重播语义：稳定帧各放行一次，名称按放行顺序连续编号。"""
    src = b"desktop roundtrip"
    comp = gzip.compress(src)
    paths = fixture_encoder.export_frames(
        comp, zlib.crc32(src) & 0xFFFFFFFF, tmp_path / "frames",
        filename="desk.bin", plain_size=len(src),
        cols=48, rows=48, bit=4, pad=3,
    )
    names = [name for name, _ in iter_desktop(capture=_captures(paths))]
    assert names == [f"desktop-{i:06d}" for i in range(1, len(paths) + 1)]


def test_iter_desktop_change_detection(tmp_path):
    """重复捕获（重播/静止画面）被变化检测吞掉，不重复放行。"""
    src = b"change detection"
    comp = gzip.compress(src)
    paths = fixture_encoder.export_frames(
        comp, zlib.crc32(src) & 0xFFFFFFFF, tmp_path / "frames",
        filename="cd.bin", plain_size=len(src),
        cols=48, rows=48, bit=4, pad=3,
    )
    # 每帧捕获 4 次（真实抓屏远高于发送帧率），仍各放行一次
    names = [name for name, _ in iter_desktop(capture=_captures(paths, repeats=4))]
    assert len(names) == len(paths)


# ---------- 缝 B 集成：desktop 通路 → 还原（含噪点注入）----------

def _frames_with_noise(paths: list[Path]):
    """抓屏夹具（含噪点）：

    1. 每个传输帧捕获两次 → 稳定放行；
    2. 好帧之间插一次随机画面（RDP 过渡画面，不稳定 → 永不放行）；
    3. 第 2 个数据帧放行后，插入它的坏帧变体（翻转一个格子）稳定两帧
       —— 闸门放行、CRC 整帧拒绝，不得污染已收好帧。
    """
    emitted = 0
    bad_drawn = False
    rng = np.random.default_rng(42)
    for p in paths:
        good = _read_gray(p)
        yield rng.integers(0, 256, size=good.shape, dtype=np.uint8)  # 过渡画面
        yield good
        yield good
        emitted += 1
        if emitted == 3 and not bad_drawn:  # paths[0] 是元数据帧，paths[3] 是数据帧 2
            bad = good.copy()
            bad[40:44, 40:44] = 255 - bad[40:44, 40:44]
            bad_drawn = True
            yield bad
            yield bad


def test_desktop_channel_restore_with_noise(tmp_path, monkeypatch):
    """desktop 通路集成：抓屏夹具（含噪点）→ 稳定闸门 → CLI 还原，字节一致。"""
    src = os.urandom(1200)
    comp = gzip.compress(src)
    paths = fixture_encoder.export_frames(
        comp, zlib.crc32(src) & 0xFFFFFFFF, tmp_path / "frames",
        filename="noisy.bin", plain_size=len(src),
        cols=48, rows=48, bit=4, pad=3,
    )
    out = tmp_path / "output"
    args = argparse.Namespace(source="desktop", dir=None,
                              region="0,0,100,100", out=out)

    r = receive_frames(args, iter_desktop(capture=_frames_with_noise(paths)))

    assert r == 0
    files = list(out.iterdir())
    assert len(files) == 1 and files[0].name == "noisy.bin"
    assert files[0].read_bytes() == src


# ---------- CLI 分派 ----------

def test_cli_desktop_bad_region_exits_2():
    with pytest.raises(SystemExit) as e:
        main(["receive", "--source", "desktop", "--region", "1,2"])
    assert e.value.code == 2


def test_cli_desktop_dispatch_passes_region(monkeypatch):
    """分派接线：--region 解析为 mss 区域并传入 iter_desktop；Ctrl+C → 退出码 1。"""
    import receiver.cli as cli

    seen = {}

    def fake_iter_desktop(region=None):
        seen["region"] = region
        yield "desktop-000001", np.zeros((10, 10), np.uint8)  # 首帧正常进入循环
        raise KeyboardInterrupt  # 模拟接收中 Ctrl+C（真实生成器异常在迭代中发生）

    monkeypatch.setattr(cli, "iter_desktop", fake_iter_desktop)
    rc = main(["receive", "--source", "desktop", "--region", "15,25,320,240"])
    assert rc == 1
    assert seen["region"] == {"left": 15, "top": 25, "width": 320, "height": 240}
