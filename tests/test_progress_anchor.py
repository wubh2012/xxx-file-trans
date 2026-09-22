"""rich 实时进度 + 目录锚定（issue #8）。

CLI 行为只测缝 B（接收端外沿）：子进程运行 `python -m receiver receive`，
断言退出码、终端文本与落盘位置；receiver.paths 是纯路径助手，参照
tests/test_sanitize.py 的做法直接单测。夹具复用 tests/fixture_encoder.py。

验收（issue #8）：
- 实时进度含帧数 / 百分比 / KB/s / 识别率 / 丢帧；
- 从任意工作目录启动，progress / output / debug 都落在脚本目录锚定位置；
- CRC 连续失败告警限流，不逐帧刷屏；
- --out 显式参数语义保持兼容（不依赖 cwd 的只有默认值）。
"""

import gzip
import os
import shutil
import subprocess
import sys
import zlib
from pathlib import Path

import cv2
import fixture_encoder

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / "venv" / "Scripts" / "python.exe"
if not PYTHON.is_file():  # 非 Windows / 无 venv 时退回当前解释器
    PYTHON = Path(sys.executable)


def run_receive(frames_dir: Path, out_dir: Path | None, *, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    """运行接收端 CLI；out_dir 为 None 时不传 --out（测默认锚定）。"""
    env = {**os.environ, "PYTHONUTF8": "1",
           # PYTHONPATH 前置 tests/_stubs：winotify 落在静默替身上（issue #23），还原成功不弹真实通知
           "PYTHONPATH": str(ROOT / "tests" / "_stubs") + os.pathsep + str(ROOT)}
    cmd = [str(PYTHON), "-m", "receiver", "receive", "--source", "images",
           "--dir", str(frames_dir)]
    if out_dir is not None:
        cmd += ["--out", str(out_dir)]
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                          encoding="utf-8")


def geometry():
    return dict(cols=48, rows=48, bit=4, pad=3)


def _export_ok(src: bytes, frames_dir: Path, filename: str) -> None:
    comp = gzip.compress(src)
    fixture_encoder.export_frames(
        comp, zlib.crc32(src) & 0xFFFFFFFF, frames_dir,
        filename=filename, plain_size=len(src), **geometry(),
    )


def test_progress_fields_in_output(tmp_path):
    """实时进度（汇总行）含帧数 / 百分比 / KB/s / 识别率 / 丢帧。"""
    src = os.urandom(3000)  # 多帧
    frames = tmp_path / "frames"
    _export_ok(src, frames, "stat.bin")

    r = run_receive(frames, tmp_path / "output")

    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert "识别率" in r.stdout
    assert "KB/s" in r.stdout
    assert "丢帧" in r.stdout
    assert "%" in r.stdout  # 百分比
    assert "已收" in r.stdout and "帧" in r.stdout  # 帧数


def test_crc_consecutive_failure_alert_throttled(tmp_path):
    """连续 30 帧 CRC 失败 → 告警限流：告警行数远少于失败帧数，
    且汇报连续失败计数；单帧失败仍必须告警（保住原断言语义）。"""
    src = b"crc storm probe"
    frames = tmp_path / "frames"
    _export_ok(src, frames, "storm.bin")  # 元数据帧 + 单数据帧
    out = tmp_path / "output"

    # 复制污染帧 30 份（翻转一个格子 → CRC 必失败），文件名有序排在末尾
    img = cv2.imread(str(sorted(frames.glob("*.png"))[-1]), cv2.IMREAD_GRAYSCALE)
    bit, pad = 4, 3
    row, col = 6, 0
    cy = (pad + row) * bit + bit // 2
    cx = (pad + col) * bit + bit // 2
    img[cy - 2 : cy + 3, cx - 2 : cx + 3] = 255 - img[cy, cx] // 128 * 255
    for i in range(2, 32):
        cv2.imwrite(str(frames / f"{i:06d}.png"), img)

    r = run_receive(frames, out)

    assert r.returncode == 1, f"stdout={r.stdout}\nstderr={r.stderr}"
    alert_lines = [ln for ln in r.stdout.splitlines() if "crc" in ln.lower()]
    assert 1 <= len(alert_lines) <= 4, f"CRC 告警应限流，实际 {len(alert_lines)} 行：{r.stdout}"
    assert any("连续" in ln for ln in alert_lines), f"告警应汇报连续失败：{alert_lines}"
    assert not out.exists() or not any(out.iterdir())


def test_anchored_paths_module():
    """路径模块：progress / output / debug 锚定脚本目录（receiver 包所在仓库根）。"""
    from receiver.paths import DEBUG_DIR, OUTPUT_DIR, PROGRESS_DIR, REPO_ROOT

    assert REPO_ROOT == ROOT
    assert PROGRESS_DIR == ROOT / "progress"
    assert OUTPUT_DIR == ROOT / "output"
    assert DEBUG_DIR == ROOT / "debug"


def test_anchored_default_out_from_foreign_cwd(tmp_path):
    """从任意工作目录启动（cwd=临时目录，不传 --out）：
    还原文件落在脚本目录锚定 output/，progress/ 目录同样锚定创建。"""
    src = (b"anchored payload, " * 16)[:237]
    frames = tmp_path / "frames"
    _export_ok(src, frames, "anchor.bin")
    foreign_cwd = tmp_path / "elsewhere"
    foreign_cwd.mkdir()

    r = run_receive(frames, None, cwd=foreign_cwd)

    try:
        assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
        dest = ROOT / "output" / "anchor.bin"
        assert dest.is_file(), f"还原文件应锚定到 {dest}，实际 stdout={r.stdout}"
        assert dest.read_bytes() == src
        assert (ROOT / "progress").is_dir(), "progress/ 应锚定创建在脚本目录"
        assert (ROOT / "debug").is_dir(), "debug/ 应锚定创建在脚本目录"
        assert not (foreign_cwd / "output").exists(), "cwd 下不得散落 output/"
    finally:
        # 清理锚定产物，不污染仓库（断言失败也必须清理）
        for name in ("output", "progress", "debug"):
            shutil.rmtree(ROOT / name, ignore_errors=True)


def test_explicit_out_semantics_unchanged(tmp_path):
    """--out 显式参数语义保持兼容：按给定值落盘，与 cwd 无关的锚定不生效。"""
    src = (b"explicit out payload, " * 12)
    frames = tmp_path / "frames"
    _export_ok(src, frames, "explicit.bin")
    out = tmp_path / "out_explicit"
    foreign_cwd = tmp_path / "elsewhere"
    foreign_cwd.mkdir()

    r = run_receive(frames, out, cwd=foreign_cwd)

    try:
        assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
        assert (out / "explicit.bin").is_file()
        assert not (ROOT / "output").exists(), "显式 --out 时不得落到锚定 output/"
    finally:
        shutil.rmtree(ROOT / "progress", ignore_errors=True)
        shutil.rmtree(ROOT / "debug", ignore_errors=True)
