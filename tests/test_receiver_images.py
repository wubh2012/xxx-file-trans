"""接收端 images 源最小闭环（issue #4）。

只测缝 B（接收端 CLI 外沿）：子进程运行 `python -m receiver receive
--source images --dir <夹具导出目录>`，断言退出码与 output/ 落盘结果。
夹具为 tests/fixture_encoder.py —— 协议的独立实现，不 mock 流水线内部。

验收（issue #4）：
- images 源解码 PNG 帧序列，还原出原文件内容；
- 几何自举全程无默认参数，与帧头交叉校验不一致整帧丢弃；
- CRC 失败整帧丢弃，不污染还原结果；
- 还原内容经 gzip 尾部 CRC + ISIZE 验证。
"""

import gzip
import hashlib
import os
import subprocess
import sys
import zlib
from pathlib import Path

import fixture_encoder

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / "venv" / "Scripts" / "python.exe"
if not PYTHON.is_file():  # 非 Windows / 无 venv 时退回当前解释器
    PYTHON = Path(sys.executable)


def run_receive(frames_dir: Path, out_dir: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONUTF8": "1"}
    return subprocess.run(
        [str(PYTHON), "-m", "receiver", "receive", "--source", "images",
         "--dir", str(frames_dir), "--out", str(out_dir)],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
    )


def geometry():
    return dict(cols=48, rows=48, bit=4, pad=3)


def test_single_frame_roundtrip(tmp_path):
    """切片 1：单帧 PNG 序列 → CLI 还原，字节与源文件一致。"""
    src = (b"hello ferry, " * 20)[:237]  # 单帧容量 262 B 内
    comp = gzip.compress(src)
    frames = tmp_path / "frames"
    fixture_encoder.export_frames(comp, zlib.crc32(src) & 0xFFFFFFFF, frames, **geometry())
    out = tmp_path / "output"

    r = run_receive(frames, out)

    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    files = list(out.iterdir())
    assert len(files) == 1, f"应落盘恰好一个还原文件，实际 {files}"
    assert files[0].read_bytes() == src


def test_multi_frame_roundtrip_sha256(tmp_path):
    """切片 2：多帧 PNG 序列 → CLI 还原，sha256 与源文件一致。"""
    src = os.urandom(3000)  # 随机数据 gzip 后近似原长 → 多帧
    comp = gzip.compress(src)
    assert len(comp) > 262, "前置失效：应分片为多帧"
    frames = tmp_path / "frames"
    fixture_encoder.export_frames(comp, zlib.crc32(src) & 0xFFFFFFFF, frames, **geometry())
    out = tmp_path / "output"

    r = run_receive(frames, out)

    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    files = list(out.iterdir())
    assert len(files) == 1
    assert hashlib.sha256(files[0].read_bytes()).digest() == hashlib.sha256(src).digest()


def test_bad_crc_frame_discarded(tmp_path):
    """切片 3：单帧画面被噪点污染（翻转一个格子）→ CRC 失败整帧丢弃，
    不落盘不污染，CLI 报非零退出码。"""
    src = b"payload under attack"
    comp = gzip.compress(src)
    frames = tmp_path / "frames"
    paths = fixture_encoder.export_frames(comp, zlib.crc32(src) & 0xFFFFFFFF, frames, **geometry())
    out = tmp_path / "output"

    # 噪点注入：翻转数据区有效字节内的一个方块颜色（任一 bit 翻转即 CRC 失败；
    # 注意补位区不参与 CRC，必须打在 DATA_LEN 覆盖的格子内）
    import cv2

    img = cv2.imread(str(paths[0]), cv2.IMREAD_GRAYSCALE)
    bit, pad = 4, 3
    row, col = 6, 0  # cell = 6*48 = 288 → byte 36，落在有效数据区内（帧头 26B 之后）
    cy = (pad + row) * bit + bit // 2
    cx = (pad + col) * bit + bit // 2
    flip = 255 if img[cy, cx] < 128 else 0  # 翻转该格颜色（保证确实翻转了 bit）
    img[cy - 2 : cy + 3, cx - 2 : cx + 3] = flip
    cv2.imwrite(str(paths[0]), img)

    r = run_receive(frames, out)

    assert r.returncode == 1, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert "crc" in r.stdout
    assert not out.exists() or not any(out.iterdir()), "坏帧不得污染还原结果"


def _write_mismatched_frame(path, *, draw, header):
    """按 draw 几何画画面、按 header 几何写帧头（CRC 对错配帧头自洽）。"""
    data = b"geometry mismatch probe"
    h = fixture_encoder.signed_header(
        0xDEADBEEF, 0, 1, data, header["chunk"], header["cols"], header["rows"],
        header["bit"], header["pad"],
    )
    fixture_encoder.render_png(h, data, draw["bit"], draw["pad"], path,
                               cols=draw["cols"], rows=draw["rows"])


def test_geometry_mismatch_frame_discarded(tmp_path):
    """切片 4a：画面画 40×40、帧头声称 48×48 → 交叉校验不一致整帧丢弃。"""
    frames = tmp_path / "frames"
    frames.mkdir()
    _write_mismatched_frame(
        frames / "000001.png",
        draw=dict(cols=40, rows=40, bit=4, pad=3),
        header=dict(cols=48, rows=48, bit=4, pad=3, chunk=262),
    )
    out = tmp_path / "output"

    r = run_receive(frames, out)

    assert r.returncode == 1, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert "geometry" in r.stdout
    assert not out.exists() or not any(out.iterdir())


def test_missing_frame_not_restored(tmp_path):
    """切片 5：缺一帧 PNG → 未收齐，不还原，非零退出。"""
    src = os.urandom(1500)  # 多帧
    comp = gzip.compress(src)
    frames = tmp_path / "frames"
    paths = fixture_encoder.export_frames(comp, zlib.crc32(src) & 0xFFFFFFFF, frames, **geometry())
    paths[-1].unlink()  # 抽掉末帧
    out = tmp_path / "output"

    r = run_receive(frames, out)

    assert r.returncode == 1, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert not out.exists() or not any(out.iterdir()), "未收齐不得落盘"


def test_corrupt_gzip_isize_fails_restore(tmp_path):
    """切片 6：各帧 CRC 均通过、但 gzip 尾部 ISIZE 被损坏 → 还原失败，非零退出。"""
    src = os.urandom(500)
    comp = bytearray(gzip.compress(src))
    comp[-4] ^= 0xFF  # 损坏尾部 ISIZE（帧 CRC 仍自洽）
    frames = tmp_path / "frames"
    fixture_encoder.export_frames(bytes(comp), zlib.crc32(src) & 0xFFFFFFFF, frames, **geometry())
    out = tmp_path / "output"

    r = run_receive(frames, out)

    assert r.returncode == 1, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert "ISIZE" in r.stdout + r.stderr
    assert not out.exists() or not any(out.iterdir()), "尾部校验不过不得落盘"


def test_bit_mismatch_frame_discarded(tmp_path):
    """切片 4b：画面按 BIT=4 画、帧头 GEO 声称 BIT=5 → 交叉校验不一致整帧丢弃。"""
    frames = tmp_path / "frames"
    frames.mkdir()
    _write_mismatched_frame(
        frames / "000001.png",
        draw=dict(cols=48, rows=48, bit=4, pad=3),
        header=dict(cols=48, rows=48, bit=5, pad=3, chunk=262),
    )
    out = tmp_path / "output"

    r = run_receive(frames, out)

    assert r.returncode == 1, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert "geometry" in r.stdout
    assert not out.exists() or not any(out.iterdir())
