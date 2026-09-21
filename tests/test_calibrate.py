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
import subprocess
import sys
import zlib
from pathlib import Path

import cv2
import numpy as np

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


def test_1px_shift_small_bit_recommends_safer_bit(tmp_path):
    """切片 4（ADR-0001 后果）：±1 像素角标偏移在小 BIT 时被放大——
    BIT=4 流整体平移 +1px 后全部帧被几何校验拒绝，探针应实测出 1px 残差，
    推荐参数识别出 BIT 不足并给出更安全值（采样窗口不越界判据 → BIT=7）。"""
    frames, paths = export_stream(
        tmp_path, os.urandom(500), filename="shift.bin", bit=4, pad=4,
    )
    for p in paths:
        shift_png(p, dx=1, dy=0)  # 整幅 +1px：角标/网格对 BIT 栅格偏移 1px

    r = run_calibrate(frames)

    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    report = last_json(r.stdout)
    assert report["measuredBit"] == 4, "探针仍应测得真实 BIT（角标完好）"
    assert report["residualPx"] == 1, "应实测出 ±1px 栅格残差"
    assert report["decodeRate"] == 0.0, "平移帧应被冻结几何校验整帧拒绝"
    assert report["BIT"] == 7, "小 BIT 不足应反推出最小安全 BIT=7"
    assert report["BIT"] > report["measuredBit"]


def test_1px_shift_large_bit_stays_recommended(tmp_path):
    """切片 5（切片 4 的对照）：同样的 ±1px 平移打在 BIT=8 上——采样窗口
    判据已满足（8−5 ≥ 2），实测 BIT 足够安全，推荐参数不虚报、维持 8。"""
    frames, paths = export_stream(
        tmp_path, os.urandom(500), filename="shift8.bin", bit=8, pad=4,
    )
    for p in paths:
        shift_png(p, dx=1, dy=0)

    r = run_calibrate(frames)

    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    report = last_json(r.stdout)
    assert report["residualPx"] == 1
    assert report["measuredBit"] == 8
    assert report["BIT"] == 8, "BIT 足够吸收 ±1px 误差时不得虚报"


def test_empty_source_reports_no_frames(tmp_path):
    """切片 6：空目录 → 无帧可统计，退出码 1（区别于用法错误 2）。"""
    frames = tmp_path / "frames"
    frames.mkdir()

    r = run_calibrate(frames)

    assert r.returncode == 1
    assert "无帧可统计" in r.stderr
