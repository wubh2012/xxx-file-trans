"""取帧源：统一迭代器接口（spec「模块划分」）。

每个源按顺序产出 (名称, 灰度画面) 二元组；images 遍历 PNG 序列目录，
desktop / video / camera 属后续票据（#9 / #10 / #13），实现时共用本接口。
"""

from pathlib import Path
from typing import Iterator

import cv2
import numpy as np


def iter_images(directory: Path) -> Iterator[tuple[str, np.ndarray]]:
    """images 源：按文件名序产出目录下全部 PNG（发送端导出布局 <seq6>.png）。"""
    for p in sorted(Path(directory).iterdir(), key=lambda p: p.name):
        if p.suffix.lower() != ".png":
            continue
        img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
        if img is None:
            print(f"警告：无法读取 {p.name}，跳过")
            continue
        yield p.name, img


def iter_source(source: str, frames_dir: Path) -> Iterator[tuple[str, np.ndarray]]:
    """按源名分派迭代器；未实现源在此显式报错（分派只留这一处）。"""
    if source == "images":
        return iter_images(frames_dir)
    raise NotImplementedError(f"源 {source} 尚未实现（desktop: #9 / video: #10 / camera: #13）")
