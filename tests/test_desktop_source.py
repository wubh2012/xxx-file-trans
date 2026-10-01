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
from receiver.pipeline import FrameRejected, decode_frame
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


def test_capture_ready_notifies_before_stable_frame_emitted():
    events = []
    image = np.zeros((16, 16), dtype=np.uint8)
    frames = iter_desktop(capture=iter([image, image]), on_ready=lambda: events.append("ready"))
    name, _ = next(frames)
    assert events == ["ready"]
    assert name == "desktop-000001"
    frames.close()


# ---------- 缝 B 集成：desktop 通路 → 还原（含噪点注入）----------

def _frames_with_noise(paths: list[Path]):
    """抓屏夹具（含噪点）：

    1. 每个传输帧捕获两次 → 稳定放行；
    2. 好帧之间插一次随机画面（RDP 过渡画面，不稳定 → 永不放行）；
    3. 第 2 个数据帧放行后，插入它的坏帧变体稳定两帧（翻转 8×8 = 4 个
       方块，损坏面积须超闸门伪影容差，否则被变化检测吞掉而非放行）
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
            bad[40:48, 40:48] = 255 - bad[40:48, 40:48]
            bad_drawn = True
            yield bad
            yield bad


def test_desktop_channel_restore_with_noise(tmp_path, monkeypatch):
    """desktop 通路集成：抓屏夹具（含噪点）→ 稳定闸门 → CLI 还原，字节一致。

    固化「闸门放行 → CRC 拒绝」路径（issue #16）：坏帧变体损坏面积超
    闸门伪影容差被放行，CRC 整帧拒绝——断言闸门放行帧中恰有 1 帧因
    CRC 被拒。若坏帧构造回归到低于容差（如 4×4），会被变化检测吞掉、
    放行帧全部解码成功，此断言随即失败。"""
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

    passed: list[tuple[str, np.ndarray]] = []  # 闸门放行帧（receive 主循环的输入）

    def record_passed():
        for name, img in iter_desktop(capture=_frames_with_noise(paths)):
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


# ---------- 收齐自动退出（issue #19，F13/验收标准 2）----------

def test_desktop_receive_stops_when_complete(tmp_path):
    """desktop 无限源收齐即还原：主循环在收齐判据满足（数据帧齐 ∧
    元数据已收）后提前退出进入还原，不等无限抓屏流耗尽，尾部画面
    不被消费（images/video 有限源行为不变）。"""
    src = b"early exit"
    comp = gzip.compress(src)
    paths = fixture_encoder.export_frames(
        comp, zlib.crc32(src) & 0xFFFFFFFF, tmp_path / "frames",
        filename="early.bin", plain_size=len(src),
        cols=48, rows=48, bit=4, pad=3,
    )
    consumed = []

    def capture():
        for p in paths:  # 每个传输帧捕获两次 → 稳定放行，全部收齐
            img = _read_gray(p)
            for _ in range(2):
                consumed.append("frame")
                yield img
        rng = np.random.default_rng(7)
        for _ in range(20):  # 收齐后的尾部画面：提前退出则不被消费
            consumed.append("tail")
            yield rng.integers(0, 256, size=_read_gray(paths[0]).shape,
                               dtype=np.uint8)

    out = tmp_path / "output"
    args = argparse.Namespace(source="desktop", dir=None,
                              region="0,0,100,100", out=out)

    r = receive_frames(args, iter_desktop(capture=capture()))

    assert r == 0
    assert (out / "early.bin").read_bytes() == src
    assert consumed.count("tail") == 0, "收齐后应停止消费抓屏流，尾部画面不被读入"


# ---------- CLI 分派 ----------

def test_cli_desktop_bad_region_exits_2():
    with pytest.raises(SystemExit) as e:
        main(["receive", "--source", "desktop", "--region", "1,2"])
    assert e.value.code == 2


def test_cli_desktop_dispatch_passes_region(monkeypatch):
    """分派接线：--region 解析为 mss 区域并传入 iter_desktop；Ctrl+C → 退出码 1。"""
    import receiver.cli as cli

    seen = {}

    def fake_iter_desktop(region=None, backend="mss"):
        seen["region"] = region
        seen["backend"] = backend
        yield "desktop-000001", np.zeros((10, 10), np.uint8)  # 首帧正常进入循环
        raise KeyboardInterrupt  # 模拟接收中 Ctrl+C（真实生成器异常在迭代中发生）

    monkeypatch.setattr(cli, "iter_desktop", fake_iter_desktop)
    rc = main(["receive", "--source", "desktop", "--region", "15,25,320,240"])
    assert rc == 1
    assert seen["region"] == {"left": 15, "top": 25, "width": 320, "height": 240}
    assert seen["backend"] == "auto"  # Windows 优先 DXGI，不可用时回退 mss

    seen.clear()
    main(["receive", "--source", "desktop", "--region", "15,25,320,240",
          "--capture", "dxgi"])
    assert seen["backend"] == "dxgi"  # --capture 直通 iter_desktop


# ---------- --region pick（issue #22）：CLI 进程内 + pick_region 注入替身 ----------

def test_cli_pick_dispatch_passes_region_and_echoes(monkeypatch, capsys):
    """--region pick → pick_region 替身被调用，region 直通 desktop 源；
    终端回显等效 `--region L,T,W,H`（下次可直接粘贴跳过框选，story 14）。"""
    import receiver.cli as cli

    seen = {}

    def fake_iter_desktop(region=None, backend="mss"):
        seen["region"] = region
        yield "desktop-000001", np.zeros((10, 10), np.uint8)
        raise KeyboardInterrupt  # 框选后 Ctrl+C 中止（真实生成器异常在迭代中发生）

    monkeypatch.setattr(cli, "pick_region",
                        lambda: {"left": 15, "top": 25, "width": 320, "height": 240})
    monkeypatch.setattr(cli, "iter_desktop", fake_iter_desktop)

    rc = cli.main(["receive", "--source", "desktop", "--region", "pick"])

    assert rc == 1
    assert seen["region"] == {"left": 15, "top": 25, "width": 320, "height": 240}
    assert "--region 15,25,320,240" in capsys.readouterr().out


def test_cli_pick_cancelled_aborts_with_exit_1(monkeypatch, capsys):
    """Esc 取消 → 明确中止提示 + 退出码 1（story 13），不静默回退整屏。"""
    import receiver.cli as cli

    def assert_not_reached(region=None):
        raise AssertionError("取消后不得进入抓屏")

    monkeypatch.setattr(cli, "pick_region", lambda: None)
    monkeypatch.setattr(cli, "iter_desktop", assert_not_reached)

    rc = cli.main(["receive", "--source", "desktop", "--region", "pick"])

    assert rc == 1
    assert "取消" in capsys.readouterr().err


def test_cli_calibrate_pick_dispatch(monkeypatch, capsys):
    """calibrate 同路共用 pick 入口（story 18）：分派传参 + 回显一致。"""
    import receiver.cli as cli

    seen = {}

    def fake_iter_desktop(region=None, backend="mss"):
        seen["region"] = region
        raise KeyboardInterrupt  # 未产出画面 → 无帧可统计（生成器异常在迭代时发生）
        yield  # 不可达：仅使本函数成为生成器，与真实 desktop 源一致

    monkeypatch.setattr(cli, "pick_region",
                        lambda: {"left": 1, "top": 2, "width": 3, "height": 4})
    monkeypatch.setattr(cli, "iter_desktop", fake_iter_desktop)

    rc = cli.main(["calibrate", "--source", "desktop", "--region", "pick"])

    assert rc == 1
    assert seen["region"] == {"left": 1, "top": 2, "width": 3, "height": 4}
    assert "--region 1,2,3,4" in capsys.readouterr().out
