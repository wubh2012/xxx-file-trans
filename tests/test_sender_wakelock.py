"""发送端播放可靠性护栏外沿测试（issue #32）。

缝 A（playwright 驱动 sender.html，file:// 直开）：只断言页面外沿行为——
Wake Lock 随播放申请 / 停止释放 / 切回可见自动重取（注入确定性假实现）、
切后台再切回的一次性节流警示、未切离不警示、不支持 Wake Lock 的环境
静默降级。真实浏览器对 Wake Lock 的授予策略因环境而异，故生命周期断言
一律基于注入的假 navigator.wakeLock（真实 API 只验证「存在即可播放」）。
未安装 playwright 时整体跳过。
"""

from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api", reason="未安装 playwright，跳过 Wake Lock 外沿测试")
from playwright.sync_api import sync_playwright  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SENDER = ROOT / "sender.html"

VIEW_W, VIEW_H = 800, 600

# 确定性假 Wake Lock：request() 计数并留存哨兵；页面 hidden 时模拟系统
# 强制释放（与真实浏览器行为一致：Wake Lock 随页面不可见自动释放）
FAKE_WAKELOCK = """(() => {
  const sentinels = [];
  const fake = {
    requestCount: 0,
    async request(type) {
      this.requestCount++;
      if (document.visibilityState === 'hidden') {
        // 与真实浏览器一致：页面不可见时 request('screen') 被拒绝
        throw new DOMException('文档不可见', 'NotAllowedError');
      }
      const sentinel = {
        type, released: false, _listeners: [],
        addEventListener(_t, fn) { this._listeners.push(fn); },
        async release() {
          if (this.released) return;
          this.released = true;
          this._listeners.forEach((f) => f());
        },
      };
      sentinels.push(sentinel);
      return sentinel;
    },
    sentinels,
  };
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') {
      sentinels.forEach((s) => s.release());
    }
  });
  Object.defineProperty(navigator, 'wakeLock', { configurable: true, value: fake });
})();"""

# 悬挂竞态假实现：request() 返回手工兑现的 Promise（模拟 await 期间用户停止播放）
PENDING_WAKELOCK = """(() => {
  let resolveRequest;
  const sentinel = {
    type: 'screen', released: false, _listeners: [],
    addEventListener(_t, fn) { this._listeners.push(fn); },
    async release() {
      if (this.released) return;
      this.released = true;
      this._listeners.forEach((f) => f());
    },
  };
  const fake = {
    requestCount: 1,
    request(_type) { return new Promise((res) => { resolveRequest = () => res(sentinel); }); },
    resolve() { resolveRequest(); },
    sentinels: [sentinel],
  };
  Object.defineProperty(navigator, 'wakeLock', { configurable: true, value: fake });
})();"""

# 无 Wake Lock API 的环境（-issue 验收：静默降级、无 JS 报错）
NO_WAKELOCK = """(() => {
  Object.defineProperty(navigator, 'wakeLock', { configurable: true, get: () => undefined });
})();"""

# 触发 visibilitychange 路径：headless 无法真把页面切后台，
# 改写 visibilityState 后派发事件（与 handler 读取口径一致）
SET_VISIBILITY = """(state) => {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state });
  document.dispatchEvent(new Event('visibilitychange'));
}"""


@pytest.fixture()
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


def make_playing_page(browser, tmp_path, init_script=None):
    """打开发送端并载入小文件进入播放态；可选注入初始化脚本。"""
    src = tmp_path / "tiny.bin"
    src.write_bytes(b"0" * 10000)  # gzip 后远小于单帧容量 → 单数据帧
    ctx = browser.new_context(viewport={"width": VIEW_W, "height": VIEW_H})
    if init_script:
        ctx.add_init_script(init_script)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(SENDER.as_uri())
    page.set_input_files("#file", str(src))
    page.wait_for_selector("body.playing", timeout=15000)
    return page, ctx, errors


# ---------- Wake Lock 生命周期（挂播放生命周期，不挂单帧渲染）----------

def test_wakelock_acquired_on_play_and_released_on_stop(browser, tmp_path):
    """开始播放即申请 screen Wake Lock；停止播放后哨兵已释放、内部状态清空。"""
    page, ctx, _ = make_playing_page(browser, tmp_path, FAKE_WAKELOCK)
    try:
        page.wait_for_function("() => window.__sender.hasWakeLock()", timeout=5000)
        assert page.evaluate("() => navigator.wakeLock.requestCount") == 1
        assert page.evaluate("() => navigator.wakeLock.sentinels[0].type") == "screen"

        page.keyboard.press("Escape")
        page.wait_for_selector("body.playing", state="detached", timeout=5000)
        assert not page.evaluate("() => window.__sender.hasWakeLock()"), "停止后内部状态应清空"
        assert page.evaluate("() => navigator.wakeLock.sentinels.every((s) => s.released)"), \
            "停止后应主动释放 Wake Lock（页面可正常熄屏）"
    finally:
        ctx.close()


def test_wakelock_reacquired_after_visibility_cycle(browser, tmp_path):
    """播放中切后台（假实现模拟系统强制释放）再切回：自动重取 Wake Lock。"""
    page, ctx, _ = make_playing_page(browser, tmp_path, FAKE_WAKELOCK)
    try:
        page.wait_for_function("() => window.__sender.hasWakeLock()", timeout=5000)

        page.evaluate(SET_VISIBILITY, "hidden")
        # 页面 hidden 后，假实现模拟系统强制释放 → 内部状态应清空
        page.wait_for_function("() => !window.__sender.hasWakeLock()", timeout=5000)

        page.evaluate(SET_VISIBILITY, "visible")
        # 切回可见应自动重取（requestCount 增加 + 内部状态非空）
        page.wait_for_function(
            "() => navigator.wakeLock.requestCount >= 2 && window.__sender.hasWakeLock()",
            timeout=5000,
        )
    finally:
        ctx.close()


# ---------- 后台节流警示（单向信道下用户无从察觉，切回须提示）----------

def test_throttle_warning_after_background_switch(browser, tmp_path):
    """播放中切走标签页再切回：状态栏给一次性警示。"""
    page, ctx, _ = make_playing_page(browser, tmp_path)
    try:
        page.evaluate(SET_VISIBILITY, "hidden")
        page.evaluate(SET_VISIBILITY, "visible")
        status = page.text_content("#status")
        assert "节流" in status and "接收端" in status, f"切回后应提示曾被节流，实际：{status}"
    finally:
        ctx.close()


def test_no_warning_without_background_switch(browser, tmp_path):
    """正常连续播放（未切离）：不得出现节流警示，状态栏保持既有文案。"""
    page, ctx, _ = make_playing_page(browser, tmp_path)
    try:
        before = page.text_content("#status")
        page.evaluate(SET_VISIBILITY, "visible")   # 无 hidden 在前，不应触发
        assert page.text_content("#status") == before, "未切离标签页不应出现节流警示"
        assert "节流" not in before
    finally:
        ctx.close()


def test_no_warning_after_stop_while_hidden(browser, tmp_path):
    """切后台后先停止播放再切回：不得把上一轮的滞留警示带进新状态。"""
    page, ctx, _ = make_playing_page(browser, tmp_path)
    try:
        page.evaluate(SET_VISIBILITY, "hidden")
        page.keyboard.press("Escape")
        page.wait_for_selector("body.playing", state="detached", timeout=5000)
        page.evaluate(SET_VISIBILITY, "visible")
        assert "节流" not in page.text_content("#status"), "已停止播放不应再提示节流"
    finally:
        ctx.close()


# ---------- 审查修复点：await 竞态 + hidden 态启动 ----------

def test_pending_request_released_when_stopped_during_await(browser, tmp_path):
    """request() 尚未落地时用户停止播放：落地后须立即释放，不留悬挂的锁
    （否则停止后屏幕无法熄屏，违背「停止播放主动释放」）。"""
    page, ctx, _ = make_playing_page(browser, tmp_path, PENDING_WAKELOCK)
    try:
        page.wait_for_timeout(300)
        assert not page.evaluate("() => window.__sender.hasWakeLock()"), "request 未落地，锁尚未持有"

        page.keyboard.press("Escape")
        page.wait_for_selector("body.playing", state="detached", timeout=5000)
        page.evaluate("() => navigator.wakeLock.resolve()")   # 此刻 promise 才落地
        # 停止后落地的锁应被立即释放
        page.wait_for_function("() => navigator.wakeLock.sentinels[0].released", timeout=5000)
        assert not page.evaluate("() => window.__sender.hasWakeLock()")
    finally:
        ctx.close()


def test_wakelock_acquired_when_playback_started_while_hidden(browser, tmp_path):
    """页面 hidden 态下启动播放（request 被浏览器拒绝，静默）：切回可见后
        应无条件补取 Wake Lock，播放不长期无锁。"""
    src = tmp_path / "tiny.bin"
    src.write_bytes(b"0" * 10000)
    ctx = browser.new_context(viewport={"width": VIEW_W, "height": VIEW_H})
    ctx.add_init_script(FAKE_WAKELOCK)
    page = ctx.new_page()
    page.goto(SENDER.as_uri())
    try:
        page.evaluate(SET_VISIBILITY, "hidden")   # 先切后台再载入文件（载入即自动开播）
        page.set_input_files("#file", str(src))
        page.wait_for_selector("body.playing", timeout=15000)
        page.wait_for_function("() => navigator.wakeLock.requestCount >= 1", timeout=5000)
        page.wait_for_timeout(200)
        assert not page.evaluate("() => window.__sender.hasWakeLock()"), "hidden 态 request 被拒，不应持有锁"

        page.evaluate(SET_VISIBILITY, "visible")
        # 切回可见应补取 Wake Lock
        page.wait_for_function("() => window.__sender.hasWakeLock()", timeout=5000)
    finally:
        ctx.close()


# ---------- 不支持 Wake Lock 的环境：静默降级 ----------

def test_unsupported_wakelock_plays_without_errors(browser, tmp_path):
    """navigator.wakeLock 缺失：播放照常启动，无 JS 报错，内部状态为空。"""
    page, ctx, errors = make_playing_page(browser, tmp_path, NO_WAKELOCK)
    try:
        page.wait_for_timeout(500)
        assert page.query_selector("body.playing") is not None, "不支持 Wake Lock 应照常播放"
        assert not page.evaluate("() => window.__sender.hasWakeLock()")
        assert errors == [], f"不应有 JS 报错：{errors}"
    finally:
        ctx.close()
