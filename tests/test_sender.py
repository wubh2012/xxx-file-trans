"""发送端静态约束测试（issue #3）。

sender.html 是 file:// 直开的零依赖单文件页面，浏览器外沿之外
没有可执行的 Python 缝；这里锁定它的最低静态约束：

- 文件存在；
- 全程零网络请求（无任何外部 URL / 外链脚本 / 网络 API）；
- CRC 自检向量（docs/protocol.md §7）内嵌于页面启动自检。
"""

import os
import re
import subprocess
import sys
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


def test_metadata_frame_building_present():
    """元数据帧构造必须存在且符合冻结布局（docs/protocol.md §4，issue #6）。

    发送端是浏览器单文件，缝 A（playwright 驱动）属 issue #5；本票以
    静态约束锁定：0xFFFFFF 哨兵、12+nameLen 布局字段、CRC 落位。
    """
    html = _html()
    assert "0xFFFFFF" in html, "缺少元数据帧号哨兵 0xFFFFFF"
    for marker in ("buildMetadataFrame", "plainSize", "compressedSize", "nameLen"):
        assert marker in html, f"缺少元数据帧布局标记: {marker}"


def test_oversized_metadata_guard_present():
    """发送端必须拦截 12+nameLen 超出 CHUNK_SIZE 的文件名（§4 接收端整帧
    丢弃的对称防御），不得静默产出每轮必被拒收的废元数据帧。"""
    html = _html()
    assert "超出单帧 CHUNK_SIZE" in html, "缺少超长文件名拦截提示"


def test_metadata_frame_cadence_present():
    """插入节奏：每轮首帧 + 每 100 帧一次（docs/protocol.md §4，issue #6）。"""
    html = _html()
    assert re.search(r"%\s*100", html), "缺少每 100 帧插入元数据帧的节奏逻辑"


def test_export_frames_feature_present():
    """F9 最小实现：发送端可导出 frames_png/<seq6>.png + frames.json（issue #5）。

    frames.json 内嵌对应接收端命令（F8），支撑闭环自测（CONTEXT.md）。
    """
    html = _html()
    assert "frames_png" in html, "缺少 frames_png 导出布局"
    assert "frames.json" in html, "缺少 frames.json 导出"
    assert "--source images" in html, "frames.json 未内嵌接收端命令（F8）"


def test_copy_receiver_command_feature_present():
    """Story 6（issue #18）：一键复制接收端命令按钮 + 同源命令生成函数。

    按钮命令随发送模式变化（desktop=全屏播放 / images=导出 PNG）；
    frames.json 的 receiverCommand 与按钮同源，不残留硬编码字面量。
    """
    html = _html()
    assert 'id="copyCmdBtn"' in html, "缺少一键复制接收端命令按钮"
    assert "buildReceiverCommand" in html, "缺少与按钮同源的命令生成函数"
    assert "navigator.clipboard" in html, "缺少剪贴板写入调用"
    # frames.json 的 receiverCommand 由生成函数产出（与按钮同源），非硬编码字符串
    assert re.search(r"receiverCommand:\s*buildReceiverCommand\(", html), \
        "frames.json receiverCommand 应由 buildReceiverCommand 生成，消除硬编码"
    for flag in ("--source desktop", "--source images", "--dir frames_png", "--out output"):
        assert flag in html, f"命令生成函数缺少 flag: {flag}"


def _receiver_receive_help_options() -> set:
    """缝 B 外沿：子进程跑 `python -m receiver receive --help`，收集其全部选项。"""
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONUTF8": "1"}
    r = subprocess.run(
        [sys.executable, "-m", "receiver", "receive", "--help"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
    )
    assert r.returncode == 0, f"receive --help 执行失败：{r.stderr}"
    return set(re.findall(r"--[a-z]+", r.stdout))


def test_receiver_command_flags_covered_by_cli_help():
    """验收标准（issue #18）：命令在接收端可直接执行——buildReceiverCommand
    用到的全部 flag 都被 `receive --help` 覆盖，CLI 契约变更即报警。"""
    html = _html()
    cmds = re.findall(r"'(python -m receiver receive[^']*)'", html)
    assert len(cmds) == 2, f"应有 desktop / images 两条接收端命令，实际 {cmds}"

    flags = set()
    for c in cmds:
        flags.update(re.findall(r"--[a-z]+", c))
    assert flags, "未从命令中解析出任何 flag"

    missing = flags - _receiver_receive_help_options()
    assert not missing, f"命令 flag 未被 receive --help 覆盖: {missing}"
