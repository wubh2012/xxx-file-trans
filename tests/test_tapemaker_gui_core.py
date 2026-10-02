"""制片 GUI 无头逻辑测试（issue #46）：default_output / validate_config /
command_line / summary_text，以及 MakeJob 真线程 + make_tape 往返。

薄壳 tapemaker_gui.pyw 不测（同 receiver_gui.pyw 惯例：纯函数拆出 +
薄壳人工验收）。未安装 ffmpeg 时 MakeJob 往返用例自动跳过（写片依赖）。
"""

import shutil
import queue
from pathlib import Path

import pytest

from tapemaker.gui_core import (MakeJob, command_line, default_output,
                                summary_text, validate_config)
from tapemaker.make import make_tape


# ---------- default_output：默认输出路径 ----------

class TestDefaultOutput:
    def test_same_dir_same_name(self, tmp_path):
        src = tmp_path / "报告.pdf"
        src.write_bytes(b"x")
        assert default_output(str(src)) == str(tmp_path / "报告.mp4")

    def test_missing_src_returns_none(self, tmp_path):
        assert default_output(str(tmp_path / "不存在.bin")) is None

    def test_empty_returns_none(self):
        assert default_output("") is None


# ---------- validate_config：开始前校验 ----------

class TestValidateConfig:
    @pytest.fixture
    def src(self, tmp_path):
        f = tmp_path / "in.bin"
        f.write_bytes(b"x")
        return str(f)

    def test_ok_with_default_bit(self, src, tmp_path):
        out = str(tmp_path / "out.mp4")
        assert validate_config(src, out) is None

    def test_missing_src(self, tmp_path):
        err = validate_config(str(tmp_path / "没有.bin"), str(tmp_path / "o.mp4"))
        assert "源文件不存在" in err

    def test_empty_output(self, src):
        assert "输出 MP4" in validate_config(src, "")

    def test_bit_range(self, src, tmp_path):
        out = str(tmp_path / "out.mp4")
        assert "BIT" in validate_config(src, out, bit_text="abc")
        assert "BIT" in validate_config(src, out, bit_text="0")
        assert "BIT" in validate_config(src, out, bit_text="16")
        assert validate_config(src, out, bit_text="8") is None

    def test_rounds_floor(self, src, tmp_path):
        out = str(tmp_path / "out.mp4")
        assert "轮次" in validate_config(src, out, rounds=1)
        assert validate_config(src, out, rounds=2) is None


# ---------- command_line：命令助手（与 CLI 参数语义一致） ----------

class TestCommandLine:
    def test_defaults_omit_resolution(self):
        cmd = command_line("a.bin", "a.mp4")
        assert cmd == 'python -m tapemaker make "a.bin" -o "a.mp4" --rounds 2 --fps 30'
        assert "--bit" not in cmd and "--resolution" not in cmd

    def test_explicit_params_all_shown(self):
        cmd = command_line("a.bin", "a.mp4", bit_text="10", rounds=3, fps=24,
                           pad=5, resolution="4k")
        assert "--bit 10" in cmd and "--rounds 3" in cmd and "--fps 24" in cmd
        assert "--pad 5" in cmd and "--resolution 4k" in cmd


# ---------- summary_text：完成文案口径 ----------

def test_summary_text_mentions_receive_command(tmp_path):
    src = tmp_path / "in.bin"
    src.write_bytes(b"x" * 64)
    out = tmp_path / "out.mp4"
    summary = make_tape(src, out)
    text = summary_text(summary)
    assert "fileId" in text and str(out) in text
    assert "--source video --video" in text and "--tape" not in text


# ---------- MakeJob：真线程 + make_tape 往返（需 ffmpeg） ----------

@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="未安装 ffmpeg，跳过制片往返")
class TestMakeJob:
    def test_done_event_and_roundtrip_ready(self, tmp_path):
        src = tmp_path / "in.bin"
        src.write_bytes(b"hello ferry" * 100)
        out = tmp_path / "out.mp4"
        events: queue.Queue = queue.Queue()
        MakeJob(str(src), str(out), events).start()
        kind, payload = events.get(timeout=60)
        assert kind == "done"
        assert payload["plain_size"] == 1100
        assert out.is_file() and out.stat().st_size > 0
        assert "fileId" in summary_text(payload)

    def test_error_event_on_missing_src(self, tmp_path):
        events: queue.Queue = queue.Queue()
        MakeJob(str(tmp_path / "没有.bin"), str(tmp_path / "o.mp4"), events).start()
        kind, message = events.get(timeout=10)
        assert kind == "error"
        assert "源文件不存在" in message
