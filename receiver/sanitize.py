"""文件名七步净化（需求文档 F14 / spec 用户故事 19）。

元数据帧中的文件名是外部输入，落盘前必须净化，落盘永远不出 output/。
本模块只做字符串层面的七步净化；resolve() 二次校验（路径锚定）在
safe_dest() 中与 out_dir 绑定执行，两级防御缺一不可。
"""

import re
import unicodedata
from pathlib import Path

MAX_NAME_BYTES = 200  # F14：200 字节（UTF-8）截断边界

# Windows 非法字符（/ \ 已在第一步作为目录分隔符剥除）
_ILLEGAL = "<>:\"|?*"
# Windows 保留名：设备名本身或「设备名.任意扩展名」，不分大小写
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$", re.IGNORECASE)
_FALLBACK = "unnamed"


def sanitize_filename(raw: str) -> str:
    """七步净化（步骤见 tests/test_sanitize.py 模块注释）。"""
    # 1 剥目录成分：\ 与 / 一律视为分隔符，只留最后一段（消灭路径穿越）
    name = re.split(r"[\\/]+", raw)[-1]
    # 2 删控制字符（Unicode Cc，含 NUL）
    name = "".join(ch for ch in name if unicodedata.category(ch) != "Cc")
    # 3 Windows 非法字符 → _
    name = name.translate({ord(c): "_" for c in _ILLEGAL})
    # 4 去首尾空格与结尾点（Windows 禁止）
    name = name.strip().rstrip(". ")
    # 5 Windows 保留名前加 _
    if _RESERVED.match(name):
        name = "_" + name
    # 6 UTF-8 200 字节截断（按字符边界，不劈多字节）
    while len(name.encode("utf-8")) > MAX_NAME_BYTES:
        name = name[:-1]
    # 7 空名兜底
    return name or _FALLBACK


def safe_dest(out_dir: Path, name: str) -> Path:
    """resolve() 二次校验：净化后的名字锚定在 out_dir 之内。

    净化是第一道防御，本函数是第二道：即便净化被绕过，路径解析后
    不在 out_dir 内即拒绝（防符号链接等残余逃逸面）。
    """
    out_dir = out_dir.resolve()
    dest = (out_dir / name).resolve()
    if not dest.is_relative_to(out_dir):
        raise ValueError(f"净化后路径仍越界：{dest} 不在 {out_dir} 之内")
    return dest
