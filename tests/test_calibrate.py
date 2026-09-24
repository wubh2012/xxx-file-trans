"""calibrate 统计模式子命令（issue #11）。

只测 CLI 外沿：子进程运行 `python -m receiver calibrate --source images
--dir <夹具目录>`，断言统计输出与推荐参数。夹具为 tests/fixture_encoder.py
（协议独立实现），±1 像素偏移用图像平移构造（ADR-0001 后果：角标测量
误差 ±1 像素在小 BIT 时被放大）。

统计模式契约：不写还原文件、不建任务目录，缺帧不影响统计完成（区别于
receive 的退出码 1）。
"""

import json
import os
import shutil
import subprocess
import sys
import zlib
from pathlib import Path

import cv2
import numpy as np
import pytest

import fixture_encoder

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / "venv" / "Scripts" / "python.exe"
if not PYTHON.is_file():  # 非 Windows / 无 venv 时退回当前解释器
    PYTHON = Path(sys.executable)


def run_calibrate(frames_dir: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONUTF8": "1"}
    return subprocess.run(
        [str(PYTHON), "-m", "receiver", "calibrate", "--source", "images",
         "--dir", str(frames_dir)],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
    )


def export_stream(tmp_path: Path, payload: bytes, *, filename: str,
                  bit: int, pad: int, cols: int = 48, rows: int = 48):
    """导出 PNG 帧序列（含元数据帧），返回 (目录, PNG 路径列表)。"""
    frames = tmp_path / "frames"
    paths = fixture_encoder.export_frames(
        payload, zlib.crc32(payload) & 0xFFFFFFFF, frames,
        filename=filename, cols=cols, rows=rows, bit=bit, pad=pad,
    )
    return frames, paths


def shift_png(path: Path, dx: int, dy: int) -> None:
    """整幅画面平移 (dx, dy) 像素，空出的边沿补黑（模拟采集几何偏差）。"""
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    out = np.zeros_like(img)
    h, w = img.shape
    out[max(dy, 0):h + min(dy, 0), max(dx, 0):w + min(dx, 0)] = \
        img[max(-dy, 0):h + min(-dy, 0), max(-dx, 0):w + min(-dx, 0)]
    cv2.imwrite(str(path), out)


def last_json(stdout: str) -> dict:
    """末行 JSON：推荐参数（回灌发送端，BIT/PAD 与 sender.html 输入项同名）。"""
    return json.loads(stdout.strip().splitlines()[-1])


def test_clean_stream_full_rates_and_recommended_params(tmp_path):
    """切片 1：clean PNG 流 → 识别率 / CRC 通过率 100%，推荐参数为实测几何。"""
    frames, _ = export_stream(
        tmp_path, os.urandom(500), filename="calib.bin", bit=4, pad=4,
    )

    r = run_calibrate(frames)

    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert "识别率" in r.stdout
    assert "CRC 通过率" in r.stdout
    report = last_json(r.stdout)
    assert report["decodeRate"] == 1.0
    assert report["crcRate"] == 1.0
    # 实测到达 FPS 仅实时源有意义（issue #29）：images 源置 None 不展示
    assert report["measuredFps"] is None
    assert "实测到达 FPS" not in r.stdout
    # 回灌发送端：BIT / PAD 与 sender.html 输入项（id=bit / id=pad）同名同界
    assert report["BIT"] == 4
    assert report["PAD"] == 4
    assert 1 <= report["BIT"] <= 15
    assert 3 <= report["PAD"] <= 15


def test_incomplete_stream_still_completes_statistics(tmp_path):
    """切片 2：统计模式不要求收齐——抽掉末帧仍 exit 0 并统计全部帧
    （对比 receive 在同场景下的退出码 1）。"""
    frames, paths = export_stream(
        tmp_path, os.urandom(1500), filename="gap.bin", bit=4, pad=4,
    )
    paths[-1].unlink()

    r = run_calibrate(frames)

    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    report = last_json(r.stdout)
    assert report["frames"] == len(paths) - 1
    assert report["decodeRate"] == 1.0


def test_rejection_breakdown_and_crc_rate(tmp_path):
    """切片 3：坏 CRC 帧计入 crc 拒绝且 CRC 通过率 < 100%；几何错配帧计入
    geometry 拒绝且不计入 CRC 分母（未到达 CRC 校验）。"""
    frames, paths = export_stream(
        tmp_path, os.urandom(500), filename="noisy.bin", bit=4, pad=4,
    )
    # 噪点注入：翻转数据帧一个格子（与 test_receiver_images 切片 3 同法）
    img = cv2.imread(str(paths[-1]), cv2.IMREAD_GRAYSCALE)
    bit, pad, row, col = 4, 4, 6, 0
    cy, cx = (pad + row) * bit + bit // 2, (pad + col) * bit + bit // 2
    img[cy - 2: cy + 3, cx - 2: cx + 3] = 255 if img[cy, cx] < 128 else 0
    cv2.imwrite(str(paths[-1]), img)
    # 几何错配帧：画面 40×40、帧头 48×48（交叉校验不一致，前置拒绝）
    mismatched = frames / "999999.png"
    data = b"geometry mismatch probe"
    h = fixture_encoder.signed_header(0xDEADBEEF, 0, 1, data, 262, 48, 48, 4, 4)
    fixture_encoder.render_png(h, data, 4, 4, mismatched, cols=40, rows=40)

    r = run_calibrate(frames)

    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    report = last_json(r.stdout)
    assert report["rejected"] == {"crc": 1, "geometry": 1}
    n = report["frames"]
    assert report["decodeRate"] == report["decoded"] / n < 1.0
    # CRC 口径：crc 帧到达了校验（计入分母），geometry 帧没有
    assert report["crcReached"] == n - 1
    assert report["crcPassed"] == n - 2
    assert report["crcRate"] == report["crcPassed"] / report["crcReached"] < 1.0


def test_1px_shift_small_bit_decodes_under_crop_tolerance(tmp_path):
    """切片 4（issue #22 语义更新）：±1px 整体平移 = 合法裁切偏移。

    旧「画面即画布」语义下，平移帧被原点校验（x0 % BIT）整帧拒绝，
    小 BIT 还会反推更安全 BIT（ADR-0001 原判例）。裁切容忍后网格由
    角标逐帧锚定、随画面一同平移，采样照常命中单元中心——BIT=4 的小
    BIT 流完整解码，推荐参数维持实测值，不虚报。"""
    frames, paths = export_stream(
        tmp_path, os.urandom(500), filename="shift.bin", bit=4, pad=4,
    )
    for p in paths:
        shift_png(p, dx=1, dy=0)  # 整幅 +1px：整体平移 = 裁切偏移，画面自洽

    r = run_calibrate(frames)

    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    report = last_json(r.stdout)
    assert report["decodeRate"] == 1.0, "整体平移=合法裁切偏移，不应整帧拒绝"
    assert report["crcRate"] == 1.0
    assert report["measuredBit"] == 4
    assert report["residualPx"] == 0, "角标完好、画面自洽，无测量残差"
    assert report["BIT"] == 4 and report["PAD"] == 4, "解码成立时不虚报推荐值"


def test_1px_shift_large_bit_decodes_under_crop_tolerance(tmp_path):
    """切片 5（切片 4 的对照）：同样的 ±1px 平移打在 BIT=8 上——采样窗口
    余量更大（8−5 ≥ 2），平移帧照常解码，推荐参数维持实测值 8。"""
    frames, paths = export_stream(
        tmp_path, os.urandom(500), filename="shift8.bin", bit=8, pad=4,
    )
    for p in paths:
        shift_png(p, dx=1, dy=0)

    r = run_calibrate(frames)

    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    report = last_json(r.stdout)
    assert report["decodeRate"] == 1.0
    assert report["residualPx"] == 0
    assert report["measuredBit"] == 8
    assert report["BIT"] == 8, "BIT 足够时不得虚报"


def test_empty_source_reports_no_frames(tmp_path):
    """切片 6：空目录 → 无帧可统计，退出码 1（区别于用法错误 2）。"""
    frames = tmp_path / "frames"
    frames.mkdir()

    r = run_calibrate(frames)

    assert r.returncode == 1
    assert "无帧可统计" in r.stderr


def test_metadata_rejected_frame_counts_as_crc_passed():
    """单测：metadata 拒绝帧的 CRC 口径——CRC 已通过、随后被元数据校验拒绝，
    应计入 CRC 分子分母两者（pipeline 校验顺序：CRC 在 metadata 校验之前）。"""
    from receiver.calibrate import CalibrationStats, tally_frame
    from receiver.protocol import FrameRejected

    stats = CalibrationStats()
    tally_frame(stats, FrameRejected("metadata", "12+nameLen 超出 CHUNK_SIZE"))
    assert stats.frames == 1
    assert stats.rejected == {"metadata": 1}
    assert stats.crc_reached == 1
    assert stats.crc_passed == 1


def test_keyboard_interrupt_returns_partial_report():
    """单测：desktop 等无限源 Ctrl+C → 返回已统计部分（interrupted=True），
    已统计帧不丢弃。"""
    import numpy as np

    from receiver.calibrate import run_calibration

    def endless_frames():
        for i in range(3):
            yield f"{i:06d}.png", np.zeros((10, 10), dtype=np.uint8)
        raise KeyboardInterrupt

    report = run_calibration(endless_frames())

    assert report["interrupted"] is True
    assert report["frames"] == 3
    assert report["decodeRate"] == 0.0
    assert report["BIT"] == 0, "无任何可测几何样本时不得虚报推荐参数"
    assert report["transfers"] == []
    assert report["measuredFps"] is not None, "到达节拍与解码成败无关，3 帧即可测"


# ---------- 缺帧/重复帧统计 + 实测到达 FPS（issue #29）----------

def test_missing_frame_counted_and_graded(tmp_path):
    """缺帧流：抽掉 1/6 数据帧 → missing=1、缺帧率 16.7% > 1% → 判级
    「警告：缺帧显著，建议降档」；重复 0、约 1 轮。"""
    frames, paths = export_stream(
        tmp_path, os.urandom(1500), filename="gap29.bin", bit=4, pad=4,
    )
    paths[2].unlink()  # paths[0] 为元数据帧，paths[2] = 数据帧 1

    r = run_calibrate(frames)

    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    report = last_json(r.stdout)
    assert len(report["transfers"]) == 1
    t = report["transfers"][0]
    assert t["totalFrames"] == 6
    assert t["received"] == 5 and t["unique"] == 5
    assert t["missing"] == 1 and t["duplicates"] == 0 and t["cycles"] == 1
    assert t["missingRate"] == pytest.approx(1 / 6)
    assert "警告：缺帧 1/6（16.7%）显著，建议降档" in r.stdout


def test_duplicate_frame_counted_and_clean_verdict(tmp_path):
    """重复帧流：数据帧 0 复制一份（换名排后）→ duplicates=1、missing=0
    → 判级「实测干净，可尝试再上一档」（单向信道零容忍下给出双向信号）。"""
    frames, paths = export_stream(
        tmp_path, os.urandom(1500), filename="dup29.bin", bit=4, pad=4,
    )
    shutil.copyfile(paths[1], frames / "999990.png")  # 数据帧 0 的完整副本

    r = run_calibrate(frames)

    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    report = last_json(r.stdout)
    t = report["transfers"][0]
    assert t["missing"] == 0 and t["duplicates"] == 1
    assert t["received"] == t["unique"] + 1
    assert "缺帧 0：该 FPS 实测干净，可尝试再上一档复测" in r.stdout


def test_multi_file_id_grouped(tmp_path):
    """多 file_id 流：会话中途换文件播放 → 各自成组，主分组 = 接收帧数
    最多的 file_id，两组互不混淆缺帧口径。"""
    frames, paths = export_stream(
        tmp_path, os.urandom(1500), filename="multi29.bin", bit=4, pad=4,
    )
    # 换文件：另一 file_id 的 2 帧小传输（几何与主流一致才能解码）
    other = 0x11223344
    for frame_no in (0, 1):
        data = b"x" * 100
        h = fixture_encoder.signed_header(other, frame_no, 2, data, 262, 48, 48, 4, 4)
        fixture_encoder.render_png(h, data, 4, 4, frames / f"99999{frame_no}.png")

    r = run_calibrate(frames)

    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    report = last_json(r.stdout)
    assert len(report["transfers"]) == 2
    main, second = report["transfers"]
    assert second["fileId"] == "0x11223344"
    assert second["totalFrames"] == 2 and second["unique"] == 2
    assert main["received"] >= second["received"], "主分组 = 接收帧数最多"
    assert main["fileId"] != second["fileId"]
    assert "传输 0x11223344：数据帧接收 2 次" in r.stdout


def test_measured_fps_injected_clock():
    """单测：实测到达 FPS 用注入时间源——假时钟每帧步进 0.02s → 中位
    50fps，p5/p95 与中位一致（节拍均匀）；分组与解码成败无关。"""
    import numpy as np

    from receiver.calibrate import run_calibration

    ticks = iter(range(1, 100))

    def frames():
        for i in range(5):
            yield f"{i:06d}.png", np.zeros((10, 10), dtype=np.uint8)

    report = run_calibration(frames(), clock=lambda: next(ticks) * 0.02)

    fps = report["measuredFps"]
    assert fps["median"] == pytest.approx(50.0)
    assert fps["p5"] == pytest.approx(50.0)
    assert fps["p95"] == pytest.approx(50.0)
    assert report["transfers"] == []
