"""测试替身：静默版 winotify（issue #23）。

winotify 转正式依赖后，`python -m receiver` 子进程测试的还原成功路径
会触发真实 Windows 弹窗（刷屏 + 每次 ~1s 的 PowerShell 开销）。本替身
置于 tests/_stubs/，由子进程测试前置 PYTHONPATH 使 import 落在此处——
工厂选择路径照常走 DesktopNotifier，仅 show() 静默（Spec 测试缝 4：
「测试注入替身，不触真实通知」）。真实弹窗人工验收，不进自动化。
"""


class Notification:
    def __init__(self, app_id, title, msg):
        self.app_id = app_id
        self.title = title
        self.msg = msg

    def set_audio(self, sound, loop=False):
        pass

    def show(self):
        pass


class audio:  # noqa: N801  # 与真实 winotify 同名命名空间（audio.Default）
    Default = "Default"
