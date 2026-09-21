"""camera 取帧源骨架（issue #13，统一迭代器接口占位）。

完整 camera 采集不在本票范围（父 spec Out of Scope）：骨架只落统一
(名称, 灰度图) 迭代器接口占位，经 iter_source 分派接入 sources 体系，
保证后续完整采集扩展时识别流水线零改动。
"""

from pathlib import Path

from receiver.cli import main
from receiver.sources import iter_source
from receiver.sources.camera import iter_camera


def test_iter_camera_yields_nothing():
    """骨架占位：迭代器可正常创建并立即耗尽，不产出任何帧。"""
    assert list(iter_camera()) == []


def test_iter_source_dispatches_camera(tmp_path):
    """统一迭代器接口：camera 经 iter_source 分派，不再 NotImplementedError。"""
    assert list(iter_source("camera", Path(tmp_path))) == []


def test_cli_camera_placeholder_message(capsys):
    """CLI 分派：--source camera 是合法取值，给出骨架提示后以用法错误码退出。"""
    rc = main(["receive", "--source", "camera"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "camera" in err and "issue #13" in err
