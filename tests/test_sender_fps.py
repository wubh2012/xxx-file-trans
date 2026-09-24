"""FPS 上限 30→60 与高 FPS 护航提示（issue #28，B1 不动协议的第一步）。

缝 A（playwright 驱动 sender.html，file:// 直开）：只断言页面外沿行为——
60 被接受、61 被钳到 60（deriveGeometry 同口径）、>30 行内警示实时显隐
（输入即提示，不等到播放）、≤30 与默认值不警示。未安装 playwright 时
整体跳过。
"""

from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api", reason="未安装 playwright，跳过 FPS 上限测试")
from playwright.sync_api import sync_playwright  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SENDER = ROOT / "sender.html"


@pytest.fixture()
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


def make_page(browser):
    ctx = browser.new_context(viewport={"width": 800, "height": 600})
    page = ctx.new_page()
    page.goto(SENDER.as_uri())
    return page, ctx


def _fill_fps(page, value: str) -> None:
    page.fill("#fps", value)
    page.dispatch_event("#fps", "input")


# ---------- 上限放开：60 接受 / 61 钳到 60 ----------

@pytest.mark.parametrize("entered,expected", [(60, 60), (61, 60)])
def test_fps_cap_60_accepts_and_clamps(browser, tmp_path, entered, expected):
    """60 被接受；61 被钳到 60（上限是输入约束，rAF 播放本身受刷新率封顶）。"""
    page, ctx = make_page(browser)
    try:
        _fill_fps(page, str(entered))
        src = tmp_path / "tiny.bin"
        src.write_bytes(b"0" * 10000)  # gzip 后远小于单帧容量 → 单数据帧
        page.set_input_files("#file", str(src))
        page.wait_for_selector("body.playing", timeout=15000)
        geo = page.evaluate("() => window.__sender.state.geo")
        assert geo["FPS"] == expected
    finally:
        ctx.close()


# ---------- 行内警示：实时显隐，不弹窗 ----------

@pytest.mark.parametrize("entered,warns", [("30", False), ("31", True), ("60", True)])
def test_fps_warning_toggles_live_on_input(browser, entered, warns):
    """>30 输入即示警（与钳位同口径），≤30 不示警；无需进入播放态。"""
    page, ctx = make_page(browser)
    try:
        _fill_fps(page, entered)
        assert page.is_visible("#fpsWarn") is warns
    finally:
        ctx.close()


def test_fps_default_10_no_warning(browser):
    """默认 FPS=10 不警示（发送端不知道对端采集方式，不替场景选默认）。"""
    page, ctx = make_page(browser)
    try:
        assert page.input_value("#fps") == "10"
        assert page.is_hidden("#fpsWarn")
    finally:
        ctx.close()
