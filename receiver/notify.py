"""完成通知（issue #12）：还原成功 → Windows 桌面通知 + 声音（winotify）。

winotify 是可选依赖：仅在本模块内导入，缺失时 default_notifier() 自动
降级为终端高亮提示（TerminalNotifier），不装 winotify 的环境照常工作。
真实弹窗无法在测试中验证，DesktopNotifier 保持薄适配（构造 → 设声音 →
show），通知发送边界由 CLI 的 notify 参数注入（receiver.cli.receive_frames）。
"""

from rich.console import Console

NOTIFY_APP_ID = "XXXFileTrans.Receiver"  # Windows 通知中心显示的应用名


def default_notifier():
    """返回完成通知实现：winotify 可用 → 桌面通知；否则终端高亮降级。"""
    try:
        import winotify  # noqa: F401  可用性探测
    except ImportError:
        return TerminalNotifier()
    return DesktopNotifier()


class DesktopNotifier:
    """winotify 薄实现：桌面通知 + 默认提示音（winotify 仅在此导入）。"""

    def __call__(self, title: str, message: str) -> None:
        from winotify import Notification, audio

        n = Notification(app_id=NOTIFY_APP_ID, title=title, msg=message)
        n.set_audio(audio.Default, loop=False)
        n.show()


class TerminalNotifier:
    """降级路径：终端高亮提示（rich），无 winotify 时的完成信号。"""

    def __init__(self, console: Console | None = None):
        self._console = console or Console()

    def __call__(self, title: str, message: str) -> None:
        self._console.print(f"[bold green]{title}[/bold green] {message}",
                            markup=True, highlight=False)
