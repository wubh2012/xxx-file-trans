"""发送端播放进度与停止时机可见性测试（issue #33）。

单向信道没有「还原完成」回执，用户最关心「什么时候能停」。画面上不能叠
任何 UI（亮色像素污染角标检测），标签页标题不进抓屏区域，是零风险的显示
位置。两层验证：

- 静态约束：标题进度更新 / 秒级节流 / 停止还原、最少播放轮数指引常量、
  信息表整文件预计时长行均内嵌于 sender.html；
- 缝 A 外沿（playwright）：播放中标题实时显示「第 N 轮 · M% · 已播 Ss」
  且轮次随循环回卷推进；Esc 停止后标题还原为播放前文本；状态文案给出
  停止时机指引；信息表出现预计总时长。

未安装 playwright 时外沿测试整体跳过。
"""

import os
import re
from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api", reason="未安装 playwright，跳过进度可见性外沿测试")
from playwright.sync_api import sync_playwright  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SENDER = ROOT / "sender.html"

DEFAULT_TITLE = "文件摆渡发送端"


def _html() -> str:
    return SENDER.read_text(encoding="utf-8")


def test_title_progress_markers_present():
    """标题进度：播放中写入 document.title、按秒节流、停止后还原（issue #33）。"""
    html = _html()
    assert "document.title" in html, "缺少标题栏进度写入"
    assert re.search(r"第 \$\{.*轮", html), "标题进度应含轮次项"
    assert re.search(r"已播", html), "标题进度应含累计时长项"
    assert re.search(r"TITLE_UPDATE_INTERVAL_MS\s*=\s*1000", html), "标题更新未按秒级节流"
    assert "titleBeforePlayback" in html, "缺少播放前标题保存 / 还原状态"


def test_min_rounds_guidance_markers_present():
    """停止时机指引：最少轮数常量为单一事实源，状态文案与播放前提示均引用它。"""
    html = _html()
    assert re.search(r"MIN_RECOMMENDED_ROUNDS\s*=\s*2", html), "缺少最少播放轮数常量（建议播满 2 轮）"
    assert "播满 ${MIN_RECOMMENDED_ROUNDS} 轮再停止" in html, "状态文案缺少轮数指引（未引用常量）"
    assert 'id="roundHint"' in html, "缺少播放前的停止时机提示元素"


def test_info_table_total_duration_markers_present():
    """信息表：「整文件预计时长」按指引轮数计算，与「每轮约 X 秒」并列。"""
    html = _html()
    assert "预计总时长" in html, "信息表缺少整文件预计时长行"
    assert "formatDuration" in html, "缺少时长格式化辅助（分钟 / 秒自适应）"


@pytest.fixture()
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


def make_playing_page(browser, tmp_path):
    """打开发送端并载入小文件进入播放态。

    用不可压缩的随机字节（约 40KB → ~30 个数据帧，单轮约 2 秒）：文件太小
    会让单轮短于一秒，「第 1 轮」的观察窗口抖动、轮次推进断言失去意义。"""
    src = tmp_path / "tiny.bin"
    src.write_bytes(os.urandom(40000))
    ctx = browser.new_context(viewport={"width": 800, "height": 600})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(SENDER.as_uri())
    page.set_input_files("#file", str(src))
    page.wait_for_selector("body.playing", timeout=15000)
    return page, errors


def test_title_shows_progress_and_rounds_advance(browser, tmp_path):
    """播放中标题实时显示「第 N 轮 · M% · 已播 Ss」，且轮次随循环回卷推进。"""
    page, errors = make_playing_page(browser, tmp_path)
    try:
        pattern = r"^第 (\d+) 轮 · \d+% · 已播 \d+s$"
        page.wait_for_function(
            f"() => new RegExp({pattern!r}).test(document.title)", timeout=5000
        )
        first_round = int(re.match(pattern, page.title()).group(1))
        assert first_round == 1, f"播放起始应为第 1 轮，实际 {first_round}"
        # 随机字节约 30 个数据帧、单轮约 2 秒，10 秒内必然回卷进入第 2 轮
        page.wait_for_function("() => document.title.startsWith('第 2 轮')", timeout=10000)
        assert errors == [], f"页面出现 JS 错误: {errors}"
    finally:
        page.context.close()


def test_title_restored_after_escape_stop(browser, tmp_path):
    """Esc 停止播放后标题还原为播放前文本（而非残留进度）。"""
    page, errors = make_playing_page(browser, tmp_path)
    try:
        page.wait_for_function(
            "() => document.title !== " + repr(DEFAULT_TITLE), timeout=5000
        )
        page.keyboard.press("Escape")
        page.wait_for_selector("body.playing", state="detached", timeout=5000)
        page.wait_for_function(
            f"() => document.title === {DEFAULT_TITLE!r}", timeout=5000
        )
        assert errors == [], f"页面出现 JS 错误: {errors}"
    finally:
        page.context.close()


def test_stop_guidance_visible_on_play_and_round_hint_before_play(browser, tmp_path):
    """状态文案给出可操作的停止时机指引；播放前 roundHint 已可见。"""
    page, errors = make_playing_page(browser, tmp_path)
    try:
        # 播放前提示（无需选文件即显示，首次使用者不看文档也知道播几轮）
        page.goto(SENDER.as_uri())
        hint = page.text_content("#roundHint")
        assert "播满 2 轮" in hint, f"播放前提示缺少轮数指引: {hint}"
        assert page.is_visible("#roundHint"), "播放前提示不可见"
        # 播放开始后状态文案给出同口径指引
        page.set_input_files("#file", tmp_path / "tiny.bin")
        page.wait_for_selector("body.playing", timeout=15000)
        status = page.text_content("#status")
        assert "播满 2 轮" in status, f"播放中状态文案缺少停止时机指引: {status}"
        assert errors == [], f"页面出现 JS 错误: {errors}"
    finally:
        page.context.close()


def test_info_table_shows_estimated_total_duration(browser, tmp_path):
    """信息表出现「预计总时长」行，按指引轮数给出整文件预计时长。

    信息表在播放中随 #ui 整体隐藏（issue #21 播放态只留画面），故在
    停止播放返回 UI 后断言（updateInfo 已填充、表格保留）。"""
    page, errors = make_playing_page(browser, tmp_path)
    try:
        page.keyboard.press("Escape")
        page.wait_for_selector("body.playing", state="detached", timeout=5000)
        page.wait_for_selector("#info:not([hidden])", timeout=5000)
        text = page.text_content("#info")
        assert "预计总时长" in text, f"信息表缺少预计总时长行: {text}"
        assert re.search(r"预计总时长\s*约 \d", text), f"预计总时长行无数值: {text}"
        assert "播满 2 轮" in text, f"预计总时长行未标注按指引轮数计: {text}"
        assert errors == [], f"页面出现 JS 错误: {errors}"
    finally:
        page.context.close()
