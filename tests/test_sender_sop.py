"""发送端两端操作 SOP 固定露出与文案改进测试（issue #36）。

三个认知成本问题的页面级修复：

- 两端 SOP checklist（发送端做什么 / 接收端做什么）固定露出在页面上，
  随播放模式联动；窗口模式固化「先开始播放，再运行接收端框选」顺序
  （与接收端 GUI gui_core 的联动提示同口径）；
- 关键提醒（勿遮挡 / 勿熄屏 / 勿切标签页 / 播满 N 轮再停）在开始播放前
  的界面常显（播放中 UI 整体隐藏，属既有行为）；
- 导出按钮文案按受众重述：导出 PNG 帧序列既是 images 源用户的真实
  交付路径，也是闭环自测素材，按用途写明。

两层验证：静态约束（标记内嵌于 sender.html）+ 缝 A 外沿（playwright，
未安装时跳过）。术语遵循 CONTEXT.md（摆渡、帧、还原、取帧源、闭环自测、
区域框选、播放模式）。
"""

import re
from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api", reason="未安装 playwright，跳过 SOP 外沿测试")
from playwright.sync_api import sync_playwright  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SENDER = ROOT / "sender.html"

DEFAULT_TITLE = "文件摆渡发送端"


def _html() -> str:
    return SENDER.read_text(encoding="utf-8")


# ---------- 静态约束 ----------

def test_sop_table_present():
    """两端 SOP checklist：两栏表（发送端 / 接收端）固定露出在页面上。"""
    html = _html()
    assert 'id="sopTable"' in html, "缺少两端 SOP 两栏表"
    for header in ("发送端", "接收端"):
        assert header in html, f"SOP 表缺少栏头: {header}"
    # SOP 步骤随播放模式联动：两种模式的步骤都定义在 PLAY_MODES 注册表内
    assert re.search(r"sop\s*:\s*\[", html), "PLAY_MODES 缺少 sop 步骤数据（应随播放模式联动）"
    # images 交付路径（导出 → 拷贝到接收侧 → images 源还原）须进 SOP 区域（Spec 轴审查补全）
    assert 'id="sopNote"' in html, "SOP 区域缺少 PNG 帧序列交付路径说明行"


def test_sop_window_mode_order_note():
    """窗口模式 SOP 固化「先开始播放，再运行接收端框选（--region pick）」顺序
    （issue #36 建议 1；与 receiver/gui_core.py 的联动提示同口径）。"""
    html = _html()
    assert "先播放，再框选" in html, "窗口模式 SOP 缺少先播放后框选的顺序固化"
    assert "--region pick" in html, "窗口模式 SOP 缺少区域框选命令提示"


def test_sop_references_receiver_gui():
    """SOP 覆盖接收端 GUI（issue #26）与命令行两条路径，首次使用不读文档即可联动。"""
    assert "receiver_gui" in _html(), "SOP 未提及接收端 GUI（receiver_gui.pyw）"


def test_pre_play_reminders_always_visible():
    """关键提醒常显（issue #36 建议 2）：勿遮挡 / 勿熄屏 / 勿切标签页 /
    播满 N 轮再停，在开始播放前的界面可见（不再是只藏在 tooltip 与一次性状态栏）。"""
    html = _html()
    assert 'id="reminders"' in html, "缺少播放前关键提醒常显块"
    for marker in ("勿遮挡", "勿熄屏", "勿切标签页"):
        assert marker in html, f"关键提醒缺少: {marker}"
    # 轮数指引引用 MIN_RECOMMENDED_ROUNDS 单一事实源（与 #33 状态文案同源）
    assert re.search(r"id=\"reminderRounds\"", html), "缺少轮数提醒条目（reminderRounds）"
    assert re.search(r"reminderRounds'\)\.textContent[^;]*MIN_RECOMMENDED_ROUNDS", html), \
        "轮数提醒应引用 MIN_RECOMMENDED_ROUNDS 常量而非硬编码"


def test_export_button_copy_audience_focused():
    """导出按钮按受众重述（issue #36 建议 3）：PNG 帧序列 = images 源还原
    的真实交付路径 + 闭环自测素材，两种用途在按钮或说明行写明。"""
    html = _html()
    assert "导出 PNG 帧序列（供 images 源还原 / 闭环自测）" in _html(), \
        "导出按钮文案未按用途重述"
    assert 'id="exportHint"' in html, "缺少导出用途说明行（区分两种用途）"


# ---------- 缝 A 外沿（playwright） ----------

@pytest.fixture()
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture()
def page(browser):
    ctx = browser.new_context(viewport={"width": 800, "height": 720})
    pg = ctx.new_page()
    pg.goto(SENDER.as_uri())
    yield pg
    ctx.close()


def test_sop_table_visible_and_mode_aware(page):
    """SOP 表加载即可见；切到窗口播放后步骤随模式联动（顺序提示 / pick 出现）。"""
    assert page.is_visible("#sopTable"), "SOP 表应加载即可见"
    heads = page.text_content("#sopTable thead")
    assert "发送端" in heads and "接收端" in heads, "SOP 表应为两栏（发送端 / 接收端）"

    fullscreen_sop = page.text_content("#sopTable tbody")
    page.click("#modeWindow")
    window_sop = page.text_content("#sopTable tbody")
    assert window_sop != fullscreen_sop, "SOP 步骤未随播放模式切换联动"
    assert "先播放" in window_sop and "后框选" in window_sop, "窗口模式 SOP 缺少先播放后框选顺序"
    assert "--region pick" in window_sop, "窗口模式 SOP 缺少 --region pick 提示"
    assert page.is_visible("#sopNote"), "SOP 表下方应露出 PNG 帧序列交付路径说明"


def test_reminders_visible_before_and_after_playback(page, tmp_path):
    """关键提醒在开始播放前可见；Esc 停止回到界面后仍然可见（常显）。"""
    assert page.is_visible("#reminders"), "播放前关键提醒不可见"
    text = page.text_content("#reminders")
    assert "勿遮挡" in text and "勿切标签页" in text
    # 直接断言轮数条目渲染结果（不能只查 "2"：ROUND_GUIDANCE 本身含 "2"）
    rounds = page.text_content("#reminderRounds")
    assert "播满 2 轮再停止" in rounds, f"轮数提醒未按 MIN_RECOMMENDED_ROUNDS 渲染: {rounds}"

    src = tmp_path / "tiny.bin"
    src.write_bytes(b"0" * 10000)
    page.set_input_files("#file", str(src))
    page.wait_for_selector("body.playing", timeout=15000)
    page.keyboard.press("Escape")
    page.wait_for_selector("body.playing", state="detached", timeout=5000)
    assert page.is_visible("#reminders"), "停止播放后关键提醒应恢复可见（常显）"


def test_export_button_label(page):
    """导出按钮文案按受众重述，说明行区分两种用途。"""
    label = page.text_content("#exportBtn")
    assert "images 源还原" in label and "闭环自测" in label, label
    assert page.is_visible("#exportHint"), "导出用途说明行应可见"
