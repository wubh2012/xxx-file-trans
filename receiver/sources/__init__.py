"""取帧源：统一迭代器接口（spec「模块划分」）。

每个源按顺序产出 (名称, 灰度画面) 二元组；images 遍历 PNG 序列目录，
desktop（#9，mss 抓屏）与 video（#10，预录视频解帧）经稳定闸门过滤，
camera（#13）为统一接口骨架占位，完整采集实现时共用本接口与
稳定帧/伪影容错逻辑。
"""

from pathlib import Path
from typing import Iterator

import cv2
import numpy as np

from receiver.sources.camera import iter_camera


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
    """按源名分派迭代器；未实现源在此显式报错。

    camera 骨架（#13）经本分派接入统一迭代器接口；CLI 侧因骨架不产出
    画面，直接提示后退出（receiver/cli.py）。desktop / video 需要额外
    参数（region / 时钟注入），由 cli 直接调用各源模块，不走本分派。
    """
    if source == "images":
        return iter_images(frames_dir)
    if source == "camera":
        return iter_camera()
    raise NotImplementedError(f"未知源 {source}")
