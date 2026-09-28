"""发送端播放模式一等化外沿测试（issue #21）。

缝 A（playwright 驱动 sender.html，file:// 直开）：只断言页面外沿行为——
模式切换（body 标注）、窗口画布自然尺寸与静默边（deviceScaleFactor
参数化覆盖 DPI，沿用 test_sender_dpi 先例）、窗口模式停止交互（点击
不停 / Esc / 停止按钮）、全屏模式既有行为不变。未安装 playwright 时
整体跳过。
"""

from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api", reason="未安装 playwright，跳过播放模式外沿测试")
from playwright.sync_api import sync_playwright  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SENDER = ROOT / "sender.html"

VIEW_W, VIEW_H = 800, 600
WINDOW_PAD_CSS = 48  # 与 sender.html 的窗口垫边一致（量级不进协议，只验证存在）


@pytest.fixture()
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


def make_playing_page(browser, tmp_path, dpr=1.0, mode="fullscreen"):
    """打开发送端，可选先点播放模式按钮，载入小文件进入播放态。"""
    src = tmp_path / "tiny.bin"
    src.write_bytes(b"0" * 10000)  # gzip 后远小于单帧容量 → 单数据帧
    ctx = browser.new_context(
        viewport={"width": VIEW_W, "height": VIEW_H},
        device_scale_factor=dpr,
    )
    page = ctx.new_page()
    page.goto(SENDER.as_uri())
    if mode == "window":
        page.click("#modeWindow")   # 播放态 UI 隐藏，须在载入文件前选模式
    page.set_input_files("#file", str(src))
    page.wait_for_selector("body.playing", timeout=15000)
    return page, ctx


def _read_canvas(page):
    return page.evaluate("""() => {
      const c = document.getElementById('stage');
      return { w: c.width, h: c.height, cssW: c.style.width, cssH: c.style.height };
    }""")


# ---------- 窗口播放：画布自然尺寸 + 静默边（story 2/3/7）----------

@pytest.mark.parametrize("dpr", [1.0, 1.25])
def test_window_mode_canvas_natural_size_with_padding(browser, tmp_path, dpr):
    """窗口模式：body 标注 window；位图 = (COLS+2×PAD)×BIT 物理像素，
    CSS 显示尺寸 = 位图 ÷ dpr（1 位图 px = 1 物理 px，不缩放）；画布四周
    垫静默边（不贴视口），状态提示标明窗口播放。"""
    page, ctx = make_playing_page(browser, tmp_path, dpr=dpr, mode="window")
    try:
        assert "window" in page.get_attribute("body", "class"), "播放态应标注窗口模式"
        geo = page.evaluate("() => window.__sender.state.geo")
        canvas = _read_canvas(page)

        assert canvas["w"] == (geo["COLS"] + 2 * geo["PAD"]) * geo["BIT"]
        assert canvas["h"] == (geo["ROWS"] + 2 * geo["PAD"]) * geo["BIT"]
        assert float(canvas["cssW"].removesuffix("px")) == pytest.approx(canvas["w"] / dpr)
        assert float(canvas["cssH"].removesuffix("px")) == pytest.approx(canvas["h"] / dpr)

        css_w = float(canvas["cssW"].removesuffix("px"))
        css_h = float(canvas["cssH"].removesuffix("px"))
        assert css_w <= VIEW_W - 2 * WINDOW_PAD_CSS + 2, "画布应窄于视口，四周垫静默边"
        assert css_h <= VIEW_H - 2 * WINDOW_PAD_CSS + 2, "画布应矮于视口，四周垫静默边"

        assert "窗口" in page.text_content("#status"), "状态应标明当前播放模式"
    finally:
        ctx.close()


# ---------- 窗口播放停止交互（story 4）----------

def test_window_mode_click_does_not_stop_but_esc_does(browser, tmp_path):
    """窗口播放：点击画面不得停止（防框选/抓屏误触），Esc 停止。"""
    page, ctx = make_playing_page(browser, tmp_path, mode="window")
    try:
        page.click("#stage")
        page.wait_for_timeout(300)
        assert page.query_selector("body.playing") is not None, "点击画面不得停止窗口播放"

        page.keyboard.press("Escape")
        page.wait_for_selector("body.playing", state="detached", timeout=5000)
    finally:
        ctx.close()


def test_window_mode_stop_button_stops(browser, tmp_path):
    """窗口播放：停止按钮可见可点，点击后停止并返回 UI。"""
    page, ctx = make_playing_page(browser, tmp_path, mode="window")
    try:
        assert page.is_visible("#stopBtn"), "窗口播放应显示停止按钮"
        page.click("#stopBtn")
        page.wait_for_selector("body.playing", state="detached", timeout=5000)
    finally:
        ctx.close()


# ---------- 全屏播放既有行为不变（story 6）----------

def test_fullscreen_mode_unchanged_click_stops(browser, tmp_path):
    """默认全屏模式：无 window 标注、无停止按钮；点击画面停止（既有交互回归）。"""
    page, ctx = make_playing_page(browser, tmp_path)
    try:
        assert "window" not in page.get_attribute("body", "class")
        assert not page.is_visible("#stopBtn"), "全屏播放不得显示窗口停止按钮"
        page.click("#stage")
        page.wait_for_selector("body.playing", state="detached", timeout=5000)
    finally:
        ctx.close()


# ---------- 全屏请求被拒：显式降级窗口播放（issue #44）----------

def test_fullscreen_denied_downgrades_to_window_with_warning(browser, tmp_path):
    """requestFullscreen 被拒（非用户手势路径，stub 模拟浏览器拒绝）时：
    不得静默吞掉落在窗口渲染却按全屏引导——应整体降级为窗口播放
    （body 标注、停止按钮、点击不停止、Esc 停止均随窗口口径），状态栏
    显式告警。"""
    src = tmp_path / "tiny.bin"
    src.write_bytes(b"0" * 10000)
    ctx = browser.new_context(viewport={"width": VIEW_W, "height": VIEW_H})
    page = ctx.new_page()
    page.goto(SENDER.as_uri())
    page.evaluate("""() => {
      // 模拟非 user gesture 下浏览器拒绝全屏请求（NotAllowedError）
      document.documentElement.requestFullscreen =
        () => Promise.reject(new DOMException('fullscreen denied', 'NotAllowedError'));
    }""")
    page.set_input_files("#file", str(src))
    page.wait_for_selector("body.playing", timeout=15000)
    try:
        assert "window" in page.get_attribute("body", "class"), "全屏被拒应降级标注窗口模式"
        assert page.is_visible("#stopBtn"), "降级窗口播放应显示停止按钮"

        page.click("#stage")
        page.wait_for_timeout(300)
        assert page.query_selector("body.playing") is not None, "降级窗口播放点击画面不得停止"

        status = page.text_content("#status")
        assert "拒绝" in status and "降级" in status, "状态栏应显式告警全屏未生效"

        page.keyboard.press("Escape")
        page.wait_for_selector("body.playing", state="detached", timeout=5000)
        assert page.get_attribute("#modeWindow", "aria-pressed") == "true", \
            "降级后模式选择应已切至窗口（SOP / 接收端命令随联动）"
    finally:
        ctx.close()


def test_fullscreen_forced_exit_midplay_warns(browser, tmp_path):
    """播放中全屏被系统强行退出（fullscreenchange 失守）时，状态栏应显式
    提示而非静默回退窗口渲染（issue #44 一致性校验）。"""
    page, ctx = make_playing_page(browser, tmp_path)  # 无头环境全屏可成功进入
    try:
        assert page.evaluate("() => !!document.fullscreenElement"), "前置：全屏应已生效"
        page.evaluate("() => document.exitFullscreen()")
        page.wait_for_function(
            "() => !document.fullscreenElement && document.getElementById('status').textContent.includes('全屏')",
            timeout=5000,
        )
        assert page.query_selector("body.playing") is not None, "失守提示不中断播放（停止由用户决定）"
    finally:
        ctx.close()


# ---------- 模式可先选后播（story 1）----------

def test_mode_selectable_before_playing_and_replay(browser, tmp_path):
    """选窗口模式 → Esc 停止 → 「开始播放」按所选模式重开（显式一等切换，
    不依赖重新选文件）。"""
    page, ctx = make_playing_page(browser, tmp_path, mode="window")
    try:
        page.keyboard.press("Escape")
        page.wait_for_selector("body.playing", state="detached", timeout=5000)

        page.click("#playBtn")
        page.wait_for_selector("body.playing", timeout=15000)
        assert "window" in page.get_attribute("body", "class"), "重播应沿用所选播放模式"
    finally:
        ctx.close()
