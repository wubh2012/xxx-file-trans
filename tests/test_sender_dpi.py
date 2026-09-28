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
PAD_DEFAULT = 3
BIT_DEFAULT_CSS = 6


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
        assert geo["BIT"] == max(round(BIT_DEFAULT_CSS * dpr), lifted), geo
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


# ---- 窗口播放画布落位（issue #42）：整数物理像素，防亚像素重采样 ----

def make_playing_page_window(browser, dpr, tmp_path, bit_css=None):
    """窗口播放模式进入播放态。先选模式再载入文件——载入完成即自动开播。"""
    src = tmp_path / "tiny.bin"
    src.write_bytes(b"0" * 10000)
    ctx = browser.new_context(
        viewport={"width": VIEW_W, "height": VIEW_H},
        device_scale_factor=dpr,
    )
    page = ctx.new_page()
    page.goto(SENDER.as_uri())
    page.click("#modeWindow")
    if bit_css is not None:
        page.fill("#bit", str(bit_css))
    page.set_input_files("#file", str(src))
    page.wait_for_selector("body.playing", timeout=15000)
    return page, ctx


@pytest.mark.parametrize("dpr,bit_css", [(1.0, None), (1.25, 7), (1.5, None)])
def test_window_canvas_lands_on_integer_physical_pixels(browser, tmp_path, dpr, bit_css):
    """窗口播放画布 CSS 落位 × dpr 必须是整数物理像素（issue #42）。

    125%/150% 缩放下 flex 居中的分数 CSS 偏移（如 49.2 CSS → 61.5 物理，
    半像素落位）使浏览器对整画布亚像素重采样：角标实测 23×22（非正方形、
    23%3≠0），被几何自举候选门槛排除，接收端恒无解。落位须按物理像素取整，
    且画布完整可见（四角角标不被裁掉）。"""
    page, ctx = make_playing_page_window(browser, dpr, tmp_path, bit_css=bit_css)
    try:
        r = page.evaluate("""() => {
          const c = document.getElementById('stage');
          const rect = c.getBoundingClientRect();
          return { left: rect.left, top: rect.top,
                   cssW: rect.width, cssH: rect.height,
                   dpr: window.devicePixelRatio,
                   innerW: innerWidth, innerH: innerHeight };
        }""")
        # 物理落位贴近整数：CSS 偏移 × dpr 距最近整数 < 1/4 物理像素。
        # 严格整数不可达（CSS 布局量化到 1/64 LayoutUnit，0.8px 步长与之
        # 不可通约），只需远离 0.5 半像素歧义区——半像素落位时画布左右边
        # 缘各自吸附到不同物理像素，渲染宽度 ≠ 位图宽度，整画布被亚像素
        # 重采样（issue #42 故障机理）
        assert abs((r["left"] * r["dpr"]) % 1 - 0) < 0.25 or \
               abs((r["left"] * r["dpr"]) % 1 - 1) < 0.25, r
        assert abs((r["top"] * r["dpr"]) % 1 - 0) < 0.25 or \
               abs((r["top"] * r["dpr"]) % 1 - 1) < 0.25, r
        # CSS 显示尺寸 × dpr 同样贴近位图整数尺寸（1 位图 px = 1 物理 px）
        assert abs(r["cssW"] * r["dpr"] - round(r["cssW"] * r["dpr"])) < 0.25, r
        assert abs(r["cssH"] * r["dpr"] - round(r["cssH"] * r["dpr"])) < 0.25, r
        # 画布完整可见：四角角标不出视口（几何自举依赖角标）
        assert r["left"] >= 0 and r["top"] >= 0, r
        assert r["left"] + r["cssW"] <= r["innerW"] + 1e-6, r
        assert r["top"] + r["cssH"] <= r["innerH"] + 1e-6, r
    finally:
        ctx.close()
