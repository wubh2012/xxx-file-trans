"""发送端物理像素画布外沿测试（issue #20）。

缝 A（playwright 驱动 sender.html，file:// 直开）：用 deviceScaleFactor
模拟 OS 显示缩放（125% / 150%），断言发送端按物理像素设计画布——

- 物理 BIT = round(CSS BIT × dpr)，且不超过帧头 4 bit 上限 15；
- 位图（canvas.width/height）= (COLS+2×PAD)×BIT 物理像素，不超过屏幕物理尺寸；
- CSS 显示尺寸 = 位图 ÷ dpr（1 位图 px = 1 物理 px，全屏播放不拉伸）；
- 帧头 GEO 高 4 bit 声明物理 BIT（接收端测得物理像素与帧头天然一致）。

dpr=1.0 时与历史行为一致（回归）。未安装 playwright 时整体跳过。
"""

from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api", reason="未安装 playwright，跳过 DPI 外沿测试")
from playwright.sync_api import sync_playwright  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SENDER = ROOT / "sender.html"

VIEW_W, VIEW_H = 800, 600
PAD_DEFAULT = 4


@pytest.fixture()
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


def make_playing_page(browser, dpr, tmp_path, bit_css=None):
    """按指定 deviceScaleFactor 打开发送端并载入小文件进入播放态。"""
    src = tmp_path / "tiny.bin"
    src.write_bytes(b"0" * 10000)  # gzip 后远小于单帧容量 → 单数据帧
    ctx = browser.new_context(
        viewport={"width": VIEW_W, "height": VIEW_H},
        device_scale_factor=dpr,
    )
    page = ctx.new_page()
    page.goto(SENDER.as_uri())
    if bit_css is not None:
        page.fill("#bit", str(bit_css))   # 播放态 UI 隐藏，须在载入文件前改参数
    page.set_input_files("#file", str(src))
    page.wait_for_selector("body.playing", timeout=15000)
    return page, ctx


def _read_geo(page):
    return page.evaluate("() => window.__sender.state.geo")


def _read_canvas(page):
    return page.evaluate("""() => {
      const c = document.getElementById('stage');
      return { w: c.width, h: c.height, cssW: c.style.width, cssH: c.style.height };
    }""")


@pytest.mark.parametrize("dpr", [1.0, 1.25, 1.5])
def test_canvas_designed_in_physical_pixels(browser, tmp_path, dpr):
    """显示缩放 ≠100% 时：几何推导、位图、CSS 显示尺寸、帧头全按物理像素。"""
    page, ctx = make_playing_page(browser, dpr, tmp_path)
    try:
        geo = _read_geo(page)
        w_phys = round(VIEW_W * dpr)
        h_phys = round(VIEW_H * dpr)

        # 物理 BIT = round(CSS BIT × dpr)（不低于 COLS/ROWS 1 字节上限的抬升值）
        lifted = max(-(-w_phys // (255 + 2 * PAD_DEFAULT)), -(-h_phys // (255 + 2 * PAD_DEFAULT)))
        assert geo["BIT"] == max(round(8 * dpr), lifted), geo
        assert geo["BIT"] <= 15
        assert (geo["COLS"] + 2 * geo["PAD"]) * geo["BIT"] <= w_phys
        assert (geo["ROWS"] + 2 * geo["PAD"]) * geo["BIT"] <= h_phys

        canvas = _read_canvas(page)
        assert canvas["w"] == (geo["COLS"] + 2 * geo["PAD"]) * geo["BIT"]
        assert canvas["h"] == (geo["ROWS"] + 2 * geo["PAD"]) * geo["BIT"]
        # CSS 显示尺寸 = 位图 ÷ dpr：1 位图 px = 1 物理 px，不拉伸
        assert float(canvas["cssW"].removesuffix("px")) == pytest.approx(canvas["w"] / dpr)
        assert float(canvas["cssH"].removesuffix("px")) == pytest.approx(canvas["h"] / dpr)

        # 帧头 GEO 高 4 bit 声明物理 BIT
        hdr_bit = page.evaluate("() => window.__sender.buildMetadataFrame().header[19] >> 4")
        assert hdr_bit == geo["BIT"]
    finally:
        ctx.close()


def test_physical_bit_rounds_to_integer(browser, tmp_path):
    """dpr 为分数且 CSS BIT × dpr 非整数时，物理 BIT 取整（保持整数 BIT，ADR-0001）。"""
    page, ctx = make_playing_page(browser, 1.25, tmp_path, bit_css=3)   # 3 × 1.25 = 3.75 → round = 4
    try:
        geo = _read_geo(page)
        assert geo["BIT"] == 4, geo
        canvas = _read_canvas(page)
        assert canvas["w"] == (geo["COLS"] + 2 * geo["PAD"]) * geo["BIT"]
    finally:
        ctx.close()
