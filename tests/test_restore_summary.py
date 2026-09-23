"""还原统计摘要（issue #25）：友好大小 / 耗时 / 明文口径速率 + 失败未完成统计。

三条缝：
- receiver.summary 纯函数外沿（friendly_size / format_rate / RestoreTimer）；
- receive_frames 注入边界（capsys 断言成功摘要行与失败路径未完成统计）；
- 缝 B：子进程 e2e stdout 摘要行（同 test_receiver_images 风格）。

验收（issue #25，grilling 共识 2026-09-23）：
- 摘要行含友好大小（MiB/KiB 二进制口径）、耗时、明文口径速率、帧 N/M、sha256；
- 计时锚点为首个 is_new 数据帧落地，首帧前时间不计入；
- 失败 / 中断路径也输出未完成统计；
- 通知文本不再出现裸字节数。
"""

import gzip
import hashlib
import os
import re
import time
import zlib
from argparse import Namespace
from pathlib import Path

import cv2
import pytest

import fixture_encoder
from receiver import paths
from receiver.cli import receive_frames
from receiver.summary import RestoreTimer, format_rate, friendly_size

CHUNK = fixture_encoder.frame_capacity(48, 48)  # 262 B（几何与 test_receiver_images 一致）


def export_ok(src: bytes, frames_dir: Path, filename: str) -> int:
    """导出完整帧序列，返回数据帧数（末帧可能短于 CHUNK）。"""
    comp = gzip.compress(src)
    fixture_encoder.export_frames(
        comp, zlib.crc32(src) & 0xFFFFFFFF, frames_dir,
        filename=filename, plain_size=len(src), cols=48, rows=48, bit=4, pad=3,
    )
    return -(-len(comp) // CHUNK)


def receive(args_out: Path, frames) -> int:
    """进程内直跑接收主循环（同 test_notify.py 注入缝）。"""
    return receive_frames(Namespace(out=args_out), frames)


@pytest.fixture
def progress_root(tmp_path, monkeypatch):
    """进程内测试把任务目录锚点改指 tmp（同 test_notify.py 夹具）。"""
    monkeypatch.setattr(paths, "PROGRESS_DIR", tmp_path / "progress")
    yield tmp_path / "progress"


# ---------- 纯函数：friendly_size（二进制口径 MiB/KiB，Windows 习惯） ----------

class TestFriendlySize:
    @pytest.mark.parametrize("n,expected", [
        (0, "0.0 KiB"),
        (2000, "2.0 KiB"),
        (1024, "1.0 KiB"),
        (1024 * 1024 - 1, "1024.0 KiB"),  # 1 MiB 差 1 字节仍是 KiB 口径
        (1024 * 1024, "1.0 MiB"),  # 恰好 1 MiB 升 MiB
        (3670016, "3.5 MiB"),
    ])
    def test_binary_units(self, n, expected):
        """≥ 1 MiB 用 MiB，否则退到 KiB；二进制口径（1024 进制）。"""
        assert friendly_size(n) == expected


# ---------- 纯函数：format_rate（明文大小 / 耗时，与大小同单位） ----------

class TestFormatRate:
    def test_mib_scale(self):
        """issue 示例：3.5 MiB / 42.7 s → 0.1 MiB/s（与大小同单位）。"""
        assert format_rate(3670016, 42.7) == "0.1 MiB/s"

    def test_kib_scale(self):
        """KiB 口径大小对应 KiB/s 速率。"""
        assert format_rate(2048, 2.0) == "1.0 KiB/s"


# ---------- RestoreTimer：锚点 = 首个 is_new 数据帧落地 ----------

class TestRestoreTimer:
    def test_not_started_reports_zero(self):
        """首帧落地前 timer 未启动：started=False、elapsed=0（不计入耗时）。"""
        timer = RestoreTimer()
        assert not timer.started
        assert timer.elapsed == 0.0

    def test_start_is_idempotent(self):
        """start 幂等：后续调用不重置锚点（与参数锁定同点、仅首个数据帧生效）。"""
        timer = RestoreTimer()
        timer.start()
        time.sleep(0.02)
        timer.start()
        elapsed = timer.elapsed
        time.sleep(0.02)
        assert timer.started
        assert timer.elapsed > elapsed, "二次 start 不得重置锚点"

    def test_elapsed_grows_after_start(self):
        timer = RestoreTimer()
        timer.start()
        first = timer.elapsed
        time.sleep(0.02)
        assert timer.elapsed >= first


# ---------- receive_frames 成功摘要行（capsys 注入边界） ----------

class TestSuccessSummary:
    def test_summary_line_fields(self, tmp_path, progress_root, capsys):
        """还原成功 → stdout 单行摘要：友好大小 / 耗时 / 明文口径速率 /
        帧 N/M / sha256（替换旧「N 字节」行，issue #25）。"""
        src = os.urandom(2000)
        frames_dir = tmp_path / "frames"
        n_data = export_ok(src, frames_dir, "done.bin")

        rc = receive(tmp_path / "out", iter_frames(frames_dir))

        assert rc == 0
        out = capsys.readouterr().out
        assert "还原完成：done.bin（2.0 KiB · " in out, f"摘要行缺友好大小：{out}"
        m_elapsed = re.search(r"耗时 (\d+\.\d) s", out)
        m_rate = re.search(r"速率 (\d+\.\d) KiB/s", out)
        m_frames = re.search(r"帧 (\d+)/(\d+)", out)
        m_sha = re.search(r"sha256 ([0-9a-f]{8})…", out)
        assert m_elapsed and m_rate and m_frames and m_sha, f"摘要行字段不全：{out}"
        assert m_frames.groups() == (str(n_data), str(n_data)), "帧数 N/M 应与数据帧数一致"
        assert m_sha.group(1) in hashlib.sha256(src).hexdigest()

    def test_time_before_first_frame_not_counted(self, tmp_path, progress_root, capsys):
        """计时锚点 = 首个 is_new 数据帧落地：首帧前的时间不计入耗时。"""
        src = b"payload under anchor"  # 单数据帧
        comp = gzip.compress(src)
        frames_dir = tmp_path / "frames"
        pngs = fixture_encoder.export_frames(
            comp, zlib.crc32(src) & 0xFFFFFFFF, frames_dir,
            filename="anchor.bin", plain_size=len(src), cols=48, rows=48, bit=4, pad=3,
        )

        def slow_frames():
            it = iter(pngs)
            time.sleep(0.4)  # 首帧落地前的等待（应不计入）
            for p in it:
                yield p.name, cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)

        rc = receive(tmp_path / "out", slow_frames())

        assert rc == 0
        out = capsys.readouterr().out
        elapsed = float(re.search(r"耗时 (\d+\.\d) s", out).group(1))
        assert elapsed < 0.3, f"首帧前 0.4 s 不应计入耗时，实测 {elapsed} s"


def iter_frames(frames_dir: Path):
    """images 源等价的 (名称, 灰度图) 迭代器（进程内注入用）。"""
    for p in sorted(frames_dir.glob("*.png")):
        yield p.name, cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)


# ---------- 失败 / 中断路径的未完成统计（暗色警告样式） ----------

class TestIncompleteSummary:
    def test_incomplete_frames_warn_stats(self, tmp_path, progress_root, capsys):
        """未收齐 → stderr 未完成统计：已耗时 + 已收 N/M 帧。"""
        src = os.urandom(2000)
        frames_dir = tmp_path / "frames"
        n_data = export_ok(src, frames_dir, "part.bin")
        for p in sorted(frames_dir.glob("*.png"))[-1:]:
            p.unlink()  # 抽掉末帧（数据帧收不全）

        rc = receive(tmp_path / "out", iter_frames(frames_dir))

        assert rc == 1
        err = capsys.readouterr().err
        m = re.search(r"还原未完成：已耗时 \d+\.\d s · 已收 (\d+)/(\d+) 帧", err)
        assert m, f"失败路径缺未完成统计：{err}"
        assert m.group(2) == str(n_data) and int(m.group(1)) < n_data

    def test_metadata_missing_warn_stats(self, tmp_path, progress_root, capsys):
        """元数据缺失（数据帧收齐）→ 同样输出未完成统计。"""
        src = os.urandom(2000)
        frames_dir = tmp_path / "frames"
        n_data = export_ok(src, frames_dir, "nometa.bin")
        meta_png = sorted(frames_dir.glob("*.png"))[0]  # export_frames：首帧为元数据帧
        meta_png.unlink()

        rc = receive(tmp_path / "out", iter_frames(frames_dir))

        assert rc == 1
        err = capsys.readouterr().err
        m = re.search(r"还原未完成：已耗时 \d+\.\d s · 已收 (\d+)/(\d+) 帧", err)
        assert m, f"元数据缺失路径缺未完成统计：{err}"
        assert m.groups() == (str(n_data), str(n_data)), "数据帧应已收齐"

    def test_restore_failure_warn_stats(self, tmp_path, progress_root, capsys):
        """帧 CRC 全过但 gzip 尾部校验不过（还原阶段失败）→ 未完成统计。"""
        src = os.urandom(2000)
        comp = bytearray(gzip.compress(src))
        comp[-4] ^= 0xFF  # 损坏尾部 ISIZE（帧 CRC 仍自洽）
        frames_dir = tmp_path / "frames"
        fixture_encoder.export_frames(
            bytes(comp), zlib.crc32(src) & 0xFFFFFFFF, frames_dir,
            filename="isize.bin", plain_size=len(src), cols=48, rows=48, bit=4, pad=3,
        )

        rc = receive(tmp_path / "out", iter_frames(frames_dir))

        assert rc == 1
        err = capsys.readouterr().err
        assert "还原失败" in err
        assert re.search(r"还原未完成：已耗时 \d+\.\d s · 已收 \d+/\d+ 帧", err), \
            f"还原失败路径缺未完成统计：{err}"

    def test_interrupt_warn_stats(self, tmp_path, progress_root, capsys):
        """Ctrl+C 中断（已收部分帧）→ 未完成统计可见（已耗时、已收帧数）。"""
        src = os.urandom(2000)
        frames_dir = tmp_path / "frames"
        n_data = export_ok(src, frames_dir, "cut.bin")
        pngs = sorted(frames_dir.glob("*.png"))

        def interrupting_frames():
            it = iter(pngs)
            for p in (next(it), next(it)):  # 元数据帧 + 数据帧 0（计时锚点落地）
                yield p.name, cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
            raise KeyboardInterrupt

        rc = receive(tmp_path / "out", interrupting_frames())

        assert rc == 1
        err = capsys.readouterr().err
        assert "接收中断" in err
        m = re.search(r"还原未完成：已耗时 \d+\.\d s · 已收 (\d+)/(\d+) 帧", err)
        assert m, f"中断路径缺未完成统计：{err}"
        assert m.groups() == ("1", str(n_data))

    def test_no_frames_still_reports_incomplete_stats(self, tmp_path, progress_root, capsys):
        """无帧落地 → 统计口径统一：已耗时 0.0 s、已收 0/? 帧也照常输出
        （spec「失败也输出」无零帧豁免，code-review 回退实现分支）。"""
        frames_dir = tmp_path / "frames"
        frames_dir.mkdir()

        rc = receive(tmp_path / "out", iter_frames(frames_dir))

        assert rc == 1
        err = capsys.readouterr().err
        assert "还原未完成：已耗时 0.0 s · 已收 0/? 帧" in err, f"{err}"


# ---------- 缝 B：子进程 e2e stdout 摘要行（issue #25 验收） ----------

class TestE2eSummaryLine:
    def test_stdout_summary_line_fields(self, tmp_path):
        """images 源子进程 e2e：stdout 摘要行含友好大小（KiB）、耗时、
        明文口径速率、帧 N/M、sha256。"""
        from test_receiver_images import run_receive

        src = os.urandom(2000)
        frames_dir = tmp_path / "frames"
        n_data = export_ok(src, frames_dir, "e2e.bin")
        out = tmp_path / "output"

        r = run_receive(frames_dir, out)

        assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
        m = re.search(
            r"还原完成：e2e\.bin（2\.0 KiB · 耗时 \d+\.\d s · "
            r"速率 \d+\.\d KiB/s · 帧 (\d+)/(\d+) · sha256 [0-9a-f]{8}…）",
            r.stdout,
        )
        assert m, f"stdout 缺规范摘要行：{r.stdout}"
        assert m.groups() == (str(n_data), str(n_data))
