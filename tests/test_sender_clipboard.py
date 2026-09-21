"""发送端「一键复制接收端命令」外沿测试（issue #18，Spec Story 6）。

缝 A（playwright 驱动 sender.html，file:// 直开）：只断言按钮点击后的
剪贴板内容与可见状态反馈，不读页面内部实现。模式切换走真实流程：
选文件 → 全屏播放（desktop 源）；点击导出（images 源）。剪贴板属环境
依赖（安全上下文 + 授权），未安装 playwright 时整体跳过。
"""

from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api", reason="未安装 playwright，跳过剪贴板外沿测试")
from playwright.sync_api import sync_playwright  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SENDER = ROOT / "sender.html"

DESKTOP_CMD = "python -m receiver receive --source desktop --out output"
IMAGES_CMD = "python -m receiver receive --source images --dir frames_png --out output"


@pytest.fixture()
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1024, "height": 768},
            permissions=["clipboard-read", "clipboard-write"],
        )
        page = context.new_page()
        page.goto(SENDER.as_uri())
        yield page
        browser.close()


def read_clipboard(page):
    return page.evaluate("() => navigator.clipboard.readText()")


def test_copy_desktop_mode_default(page):
    """默认（全屏播放）模式：点击按钮得到 desktop 源命令，并给出可见成功反馈。"""
    page.click("#copyCmdBtn")
    assert read_clipboard(page) == DESKTOP_CMD
    status = page.text_content("#status")
    assert "已复制" in status and "--source desktop" in status, status


def test_copy_follows_send_mode(page, tmp_path):
    """命令随发送模式切换：加载文件→播放（desktop 源）；点击导出→images 源。"""
    src = tmp_path / "tiny.bin"
    src.write_bytes(b"0" * 10000)  # gzip 后远小于单帧容量 → 单数据帧，导出 3 个文件
    page.set_input_files("#file", str(src))
    page.wait_for_selector("body.playing", timeout=15000)
    page.keyboard.press("Escape")

    page.click("#copyCmdBtn")
    assert read_clipboard(page) == DESKTOP_CMD

    # 导出（自动化环境走下载回退）：完成后状态行可见，模式随之切到 images
    page.click("#exportBtn")
    page.wait_for_selector("#status:has-text('已导出')", timeout=15000)
    page.click("#copyCmdBtn")
    assert read_clipboard(page) == IMAGES_CMD


def test_copy_failure_visible_error(page):
    """剪贴板写入失败（如非安全上下文限制）必须给出可见错误而非静默（issue #18）。"""
    page.evaluate(
        "() => { navigator.clipboard.writeText = "
        "() => Promise.reject(new DOMException('denied', 'NotAllowedError')); }"
    )
    page.click("#copyCmdBtn")
    status = page.text_content("#status")
    assert status.startswith("复制失败"), status
    assert "--source desktop" in status, "错误提示应附带命令文本，便于手动复制"
    color = page.evaluate(
        "() => getComputedStyle(document.getElementById('status')).color"
    )
    assert color == "rgb(255, 102, 102)", "错误状态应为红色（setStatus isErr 样式）"
