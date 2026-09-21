"""发送端静态约束测试（issue #3）。

sender.html 是 file:// 直开的零依赖单文件页面，浏览器外沿之外
没有可执行的 Python 缝；这里锁定它的最低静态约束：

- 文件存在；
- 全程零网络请求（无任何外部 URL / 外链脚本 / 网络 API）；
- CRC 自检向量（docs/protocol.md §7）内嵌于页面启动自检。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SENDER = ROOT / "sender.html"


def test_sender_html_exists():
    assert SENDER.is_file(), "根目录缺少 sender.html"


def _html() -> str:
    return SENDER.read_text(encoding="utf-8")


def test_zero_external_references():
    """file:// 直开、零网络请求：不允许任何外部资源或网络 API。"""
    html = _html()
    assert not re.search(r"https?://", html), "页面含外部 URL"
    forbidden = [
        "<script src=",  # 外链脚本
        "<link",  # 外链样式/图标
        "@import",  # CSS 导入
        "fetch(",  # 网络 API
        "XMLHttpRequest",
        "WebSocket",
        "EventSource",
        "sendBeacon",
        "navigator.send",
        "import(",  # 动态模块加载
    ]
    for token in forbidden:
        assert token not in html, f"页面含网络相关标记: {token}"


def test_crc_self_check_vector_embedded():
    """协议自检向量必须内嵌于页面启动自检（docs/protocol.md §7）。"""
    html = _html()
    assert "123456789" in html
    assert re.search(r"0xCBF43926", html, re.IGNORECASE), "缺少自检向量期望值 0xCBF43926"
