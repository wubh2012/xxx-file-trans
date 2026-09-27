"""端到端 pytest 闭环（issue #5）：缝 A 产物直喂缝 B。

缝 A（发送端浏览器外沿）：playwright 驱动 sender.html（file:// 直开）完成
选择文件 → 点击导出按钮，产出 frames_png/<seq6>.png + frames.json（F9 最小
实现；无目录选择权限时逐文件下载回退，下载即导出的外沿行为）。
缝 B（接收端 CLI 外沿）：子进程运行 `python -m receiver receive --source
images --dir frames_png`，断言还原文件 sha256 与源文件一致——验收标准第 1
条（双端协议一致）的自动化。

只测外部行为：导出目录内容、output/ 落盘结果，不 mock 流水线内部。
playwright 属环境依赖（浏览器二进制），未安装时本模块整体跳过。
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api", reason="未安装 playwright，跳过端到端闭环")
from playwright.sync_api import sync_playwright  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SENDER = ROOT / "sender.html"
PYTHON = ROOT / "venv" / "Scripts" / "python.exe"
if not PYTHON.is_file():  # 非 Windows / 无 venv 时退回当前解释器
    PYTHON = Path(sys.executable)

SOURCE_BYTES = os.urandom(12000)  # BIT/PAD 默认优化后仍保证至少 3 个数据帧，覆盖双帧 FEC 恢复


def run_receive(frames_dir: Path, out_dir: Path) -> subprocess.CompletedProcess:
    """缝 B：与 test_receiver_images 同款子进程外沿，独立成文避免测试互耦。"""
    env = {**os.environ, "PYTHONUTF8": "1",
           # PYTHONPATH 前置 tests/_stubs：winotify 落在静默替身上（issue #23），还原成功不弹真实通知
           "PYTHONPATH": str(ROOT / "tests" / "_stubs") + os.pathsep + str(ROOT)}
    return subprocess.run(
        [str(PYTHON), "-m", "receiver", "receive", "--source", "images",
         "--dir", str(frames_dir), "--out", str(out_dir)],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
    )


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    """缝 A：驱动发送端页面完成选择文件 → 导出，返回 (导出目录, 源文件字节)。

    断言只落在页面外沿：body.playing（处理完成开始播放）、Escape 停止后
    点击导出按钮、捕获逐文件下载。文件数由协议节奏推导（数据帧 + 按节奏
    插入的元数据帧），不读页面内部实现。
    """
    src_file = tmp_path_factory.mktemp("src") / "ferry.bin"
    src_file.write_bytes(SOURCE_BYTES)
    export_root = tmp_path_factory.mktemp("export")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 1024, "height": 768}, accept_downloads=True,
        )
        page.goto(SENDER.as_uri())
        page.set_input_files("#file", str(src_file))
        page.wait_for_selector("body.playing", timeout=15000)  # 文件处理完成
        page.keyboard.press("Escape")                          # 停止播放，露出导出按钮

        total_frames = page.evaluate("() => window.__sender.state.totalFrames")
        # 文件数 = 数据帧 + 元数据帧 + FEC 校验帧 + frames.json
        fec = (total_frames + 31) // 32 * 2
        expected = total_frames + (total_frames + 99) // 100 + fec + 1

        page.click("#exportBtn")
        downloads = []
        with page.expect_download(timeout=15000) as dl:
            pass
        downloads.append(dl.value)
        for _ in range(expected - 1):
            with page.expect_download(timeout=15000) as dl:
                pass
            downloads.append(dl.value)
        # 导出目录落盘：frames_png/<seq6>.png + frames.json（F9 布局）
        frames_png = export_root / "frames_png"
        frames_png.mkdir()
        for d in downloads:
            if d.suggested_filename == "frames.json":
                d.save_as(export_root / "frames.json")
            else:
                d.save_as(frames_png / d.suggested_filename)
        browser.close()

    assert len(downloads) == expected, \
        f"应导出 {expected} 个文件（数据帧 + 元数据帧 + frames.json），实际 {len(downloads)}"
    return export_root


def test_export_layout_and_frames_json(exported):
    """切片 1：导出目录 = frames_png/<seq6>.png 序列 + frames.json（F9），
    frames.json 内嵌接收端命令（F8）且与画面几何自洽。"""
    export_root = exported
    frames_json = json.loads((export_root / "frames.json").read_text(encoding="utf-8"))

    assert frames_json["fileName"] == "ferry.bin"
    assert frames_json["plainSize"] == len(SOURCE_BYTES)
    assert frames_json["totalExported"] == (
        frames_json["totalFrames"] + (frames_json["totalFrames"] + 99) // 100
        + frames_json["fec"]["totalParityFrames"]
    ), "PNG 帧数应等于数据帧 + 元数据帧 + FEC 校验帧"
    assert frames_json["receiverCommand"] == (
        "python -m receiver receive --source images --dir frames_png --out output"
    ), "receiverCommand 应为与复制按钮同源的 images 模式命令（F8，issue #18）"

    geo = frames_json["geometry"]
    assert {"COLS", "ROWS", "BIT", "PAD"} <= set(geo), "frames.json 缺几何参数"

    pngs = sorted((export_root / "frames_png").iterdir())
    assert len(pngs) == frames_json["totalExported"]
    assert all(re.fullmatch(r"\d{6}\.png", p.name) for p in pngs), "应为 <seq6>.png 命名"

    import cv2
    # 首帧是元数据帧（每轮首帧先插元数据，§4），画面尺寸须与几何参数自洽
    first = cv2.imread(str(pngs[0]), cv2.IMREAD_GRAYSCALE)
    assert first.shape == (
        (geo["ROWS"] + 2 * geo["PAD"]) * geo["BIT"],
        (geo["COLS"] + 2 * geo["PAD"]) * geo["BIT"],
    )


def test_ferried_file_sha256_roundtrip(exported, tmp_path):
    """切片 2：缝 A 产物直喂缝 B，还原文件 sha256 == 源文件 sha256。"""
    export_root = exported
    out = tmp_path / "output"

    r = run_receive(export_root / "frames_png", out)

    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    files = list(out.iterdir())
    assert len(files) == 1, f"应落盘恰好一个还原文件，实际 {files}"
    assert files[0].name == "ferry.bin"
    assert hashlib.sha256(files[0].read_bytes()).digest() \
        == hashlib.sha256(SOURCE_BYTES).digest(), "还原文件 sha256 与源文件不一致"


def test_fec_recovers_two_missing_browser_data_frames(exported, tmp_path):
    """发送端真实浏览器产出的两个校验帧能恢复同组两个缺失数据帧。"""
    import cv2

    from receiver.pipeline import decode_frame
    from receiver.protocol import FLAGS_FEC, FRAME_NO_METADATA

    damaged = tmp_path / "frames_missing_two"
    damaged.mkdir()
    removed = []
    for src in sorted((exported / "frames_png").iterdir()):
        img = cv2.imread(str(src), cv2.IMREAD_GRAYSCALE)
        frame = decode_frame(img)
        is_data = (frame.header.frame_no != FRAME_NO_METADATA
                   and not (frame.header.flags & FLAGS_FEC))
        if is_data and frame.header.frame_no in (0, 2):
            removed.append(frame.header.frame_no)
            continue
        shutil.copy2(src, damaged / src.name)
    assert removed == [0, 2]

    out = tmp_path / "fec_output"
    result = run_receive(damaged, out)

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    restored = out / "ferry.bin"
    assert restored.is_file()
    assert hashlib.sha256(restored.read_bytes()).digest() \
        == hashlib.sha256(SOURCE_BYTES).digest()
