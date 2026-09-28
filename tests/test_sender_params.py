"""场景预设档 + 配置记忆 + 播放中参数锁定外沿测试（issue #34）。

缝 A（playwright 驱动 sender.html，file:// 直开）：只断言页面外沿行为——
一键预设填参（benchmark 口径）且仍可手动微调、刷新后恢复上次配置
（file:// localStorage 同 context 跨 reload）、存储不可用时静默降级为
默认值、播放中参数锁定 / 停止后恢复。未安装 playwright 时整体跳过。
"""

from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api", reason="未安装 playwright，跳过参数体验测试")
from playwright.sync_api import sync_playwright  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SENDER = ROOT / "sender.html"

LOCKED_IDS = ["bit", "pad", "fps", "winW", "winH",
              "modeFullscreen", "modeWindow", "presetDesktop", "presetCamera"]


@pytest.fixture()
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


def make_page(browser, blocked_storage=False):
    ctx = browser.new_context(viewport={"width": 800, "height": 600})
    page = ctx.new_page()
    if blocked_storage:
        # 模拟隐私模式 / 禁用站点存储：任何 localStorage 访问即抛错
        page.add_init_script(
            "Object.defineProperty(window, 'localStorage',"
            " { get() { throw new Error('storage blocked'); } });")
    page.goto(SENDER.as_uri())
    return page, ctx


# ---------- 场景预设档：一键填参（benchmark 口径），仍可手动微调 ----------

def test_desktop_preset_fills_benchmark_values_and_tweakable(browser):
    """desktop 抓屏预设：窗口播放 + BIT 6 / FPS 20（实投建议区间上沿）；
    预设后手动改 FPS 立即生效（预设只是填参）。"""
    page, ctx = make_page(browser)
    try:
        page.click("#presetDesktop")
        assert page.input_value("#fps") == "20"
        assert page.input_value("#bit") == "6"
        assert page.get_attribute("#modeWindow", "aria-pressed") == "true"
        assert page.is_visible("#windowSizeRow")

        page.fill("#fps", "17")   # 预设后仍可手动微调
        assert page.input_value("#fps") == "17"
    finally:
        ctx.close()


def test_camera_preset_fills_conservative_values(browser):
    """camera 拍摄预设：全屏播放 + BIT 8（benchmark 稳定档同款）/ FPS 10 保守。"""
    page, ctx = make_page(browser)
    try:
        page.click("#presetCamera")
        assert page.input_value("#fps") == "10"
        assert page.input_value("#bit") == "8"
        assert page.get_attribute("#modeFullscreen", "aria-pressed") == "true"
        assert page.is_hidden("#windowSizeRow")
    finally:
        ctx.close()


# ---------- 配置记忆：刷新恢复上次 BIT/FPS/播放模式（含窗口尺寸） ----------

def test_config_memory_restores_after_reload(browser):
    """改动参数与模式 → 刷新 → 全部恢复（file:// localStorage 跨 reload）。"""
    page, ctx = make_page(browser)
    try:
        page.fill("#bit", "5")
        page.fill("#fps", "12")
        page.click("#modeWindow")
        page.fill("#winW", "400")
        page.fill("#winH", "300")
        page.reload()
        assert page.input_value("#bit") == "5"
        assert page.input_value("#fps") == "12"
        assert page.get_attribute("#modeWindow", "aria-pressed") == "true"
        assert page.input_value("#winW") == "400"
        assert page.input_value("#winH") == "300"
        assert page.is_visible("#windowSizeRow")
        assert page.is_hidden("#fpsWarn"), "恢复 FPS ≤ 30 不得误触警示"
    finally:
        ctx.close()


def test_config_memory_restores_high_fps_warning_state(browser):
    """恢复 > 30 的 FPS 时警示随之显示（与输入实时显隐同口径）。"""
    page, ctx = make_page(browser)
    try:
        page.fill("#fps", "45")
        page.reload()
        assert page.input_value("#fps") == "45"
        assert page.is_visible("#fpsWarn")
    finally:
        ctx.close()


def test_storage_blocked_falls_back_to_defaults(browser):
    """隐私模式 / 禁用存储：页面正常工作，用现有默认值；改动不报错。"""
    page, ctx = make_page(browser, blocked_storage=True)
    try:
        assert page.input_value("#fps") == "15"
        assert page.input_value("#bit") == "6"
        assert page.get_attribute("#modeFullscreen", "aria-pressed") == "true"
        page.fill("#fps", "31")   # 保存路径静默失败，页面不得崩
        assert page.is_visible("#fpsWarn")
        assert "自检" in page.text_content("#status")
    finally:
        ctx.close()


# ---------- 播放中参数锁定：置灰 + 提示，停止后恢复 ----------

def test_params_locked_during_playback_unlocked_after_stop(browser, tmp_path):
    """播放中参数 / 预设 / 模式控件置灰并解除提示的 hidden；Esc 停止后恢复。
    锁定提示位于播放态整体隐藏的 #ui 内，用 hidden 属性断言显隐契约
    （is_visible 会因父容器 display:none 恒为 False，与锁定无关）。"""
    src = tmp_path / "tiny.bin"
    src.write_bytes(b"0" * 10000)  # gzip 后远小于单帧容量 → 单数据帧
    page, ctx = make_page(browser)
    try:
        page.set_input_files("#file", str(src))
        page.wait_for_selector("body.playing", timeout=15000)
        for i in LOCKED_IDS:
            assert page.is_disabled(f"#{i}"), f"播放中 #{i} 应被置灰"
        assert not page.get_attribute("#paramLockHint", "hidden"), "播放中应显示锁定提示"

        page.keyboard.press("Escape")
        page.wait_for_selector("body.playing", state="detached", timeout=5000)
        for i in LOCKED_IDS:
            assert not page.is_disabled(f"#{i}"), f"停止后 #{i} 应恢复可编辑"
        assert page.get_attribute("#paramLockHint", "hidden") is not None, "停止后提示应收起"
    finally:
        ctx.close()
