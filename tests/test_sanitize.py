"""文件名七步净化表驱动单测（issue #6 / F14 / spec 验收标准 9）。

纯函数直测 receiver.sanitize.sanitize_filename（spec「测试策略」明文
指定的表驱动用例）；resolve() 二次校验属缝 B 端到端场景，在
test_receiver_metadata.py 中经 CLI 验证。

七步（docs/specs/0001 / 需求文档 F14）：
  1 剥目录成分（\\ 与 / 一律视为分隔符）→ 消灭路径穿越
  2 删控制字符（Unicode Cc，含 NUL）
  3 替换 Windows 非法字符 <>:"|?* → _
  4 去首尾空格与结尾点
  5 Windows 保留名前加 _（CON/PRN/AUX/NUL/COM1-9/LPT1-9，不分大小写，含带扩展名形态）
  6 UTF-8 200 字节截断（按字符边界，不劈多字节）
  7 空名兜底 unnamed
"""

import pytest

from receiver.sanitize import MAX_NAME_BYTES, sanitize_filename

CASES = [
    # (输入, 期望输出, 场景)
    ("../../evil.txt", "evil.txt", "路径穿越：正斜杠"),
    ("..\\..\\x", "x", "路径穿越：反斜杠"),
    ("a/b\\c\\report.pdf", "report.pdf", "混合分隔符取最后一段"),
    ("C:\\Users\\evil\\secret.zip", "secret.zip", "盘符路径"),
    ("report\x00.txt", "report.txt", "NUL 控制字符删除"),
    ("na\x1fme.bin", "name.bin", "其他控制字符删除"),
    ('bad<>:"|?*name', "bad_______name", "Windows 非法字符替换"),
    ("trailing name . .. ", "trailing name", "首尾空格与结尾点"),
    ("CON", "_CON", "保留名"),
    ("con", "_con", "保留名不分大小写"),
    ("CON.txt", "_CON.txt", "保留名带扩展名"),
    ("lpt9.log", "_lpt9.log", "LPT 保留名带扩展名"),
    ("content.txt", "content.txt", "仅前缀命中保留名不算保留名"),
    ("报告 最终版.pdf", "报告 最终版.pdf", "合法中文名原样保留"),
    ("..", "unnamed", "纯点净化后为空 → 兜底名"),
    ("   ", "unnamed", "纯空格净化后为空 → 兜底名"),
    ("", "unnamed", "空名兜底"),
]


@pytest.mark.parametrize(("raw", "expected", "scene"), CASES, ids=[c[2] for c in CASES])
def test_sanitize_table(raw, expected, scene):
    assert sanitize_filename(raw) == expected, scene


def test_200_byte_boundary_exact():
    """恰好 200 UTF-8 字节的名字不被截断。"""
    name = "a" * (MAX_NAME_BYTES - 3) + "中"  # 197×1 + 3 = 200 字节
    assert len(name.encode("utf-8")) == MAX_NAME_BYTES
    assert sanitize_filename(name) == name


def test_overlong_truncated_on_char_boundary():
    """超长截断到 ≤200 字节，且不劈开多字节字符。"""
    name = "中" * 150  # 450 字节
    out = sanitize_filename(name)
    assert len(out.encode("utf-8")) <= MAX_NAME_BYTES
    assert out == "中" * (MAX_NAME_BYTES // 3)  # 按整字符保留，无乱码
