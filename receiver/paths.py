"""目录锚定（需求 N4）：progress / output / debug 锚定脚本目录，不依赖 cwd。

脚本目录 = receiver 包所在仓库根；从任意工作目录启动，运行产物都落在
锚定位置，不在 cwd 散落文件。显式 --out 语义保持兼容（按用户给定值，
仅默认值改为锚定 output/）。
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def anchor(*parts: str) -> Path:
    """返回锚定到脚本目录（receiver 包所在仓库根）的路径。"""
    return REPO_ROOT.joinpath(*parts)


OUTPUT_DIR = anchor("output")  # 还原输出（F13）
PROGRESS_DIR = anchor("progress")  # 断点续传任务目录（F12；meta.json 持久化属 #7）
DEBUG_DIR = anchor("debug")  # 调试画面留存（后续票据）
