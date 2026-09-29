"""制片编排核心（issue #46）：make 子命令与 GUI 共用的无头实现。

从 cli.py 拆出的动机：GUI（tapemaker_gui.pyw）与 CLI 必须走同一条
制片路径，防止双实现漂移——本模块只做编排（读文件 → gzip → 封帧 →
写带 → 返回摘要），不做打印与进程退出码，错误一律 ValueError /
RuntimeError 交调用方决定呈现方式。
"""

from __future__ import annotations

import gzip
import time
import zlib
from pathlib import Path

from receiver.protocol import startup_self_check
from tapemaker.frames import (
    DEFAULT_BIT,
    RESOLUTIONS,
    Frame,
    Geometry,
    build_round,
    derive_geometry,
)
from tapemaker.render import TapeWriter, render_frame

MAKE_CRF = 12  # 制片侧码率档：低 CRF 保角标边缘锐利，二压才是主要损失源


def write_tape(out_path: Path, frames: list[Frame], geo: Geometry, fps: int,
               rounds: int, *, crf: int = MAKE_CRF, gop: int = 1) -> None:
    """帧序列连播 rounds 轮写入 MP4（make / calibrate 共用写片核心）。"""
    writer = TapeWriter(out_path, geo, fps, crf=crf, gop=gop)
    try:
        for _ in range(rounds):
            for frame in frames:
                writer.write(render_frame(frame, geo))
        writer.close()
    except Exception as e:
        raise writer.fail(f"制片失败：{e}") from e


def make_tape(src: Path, output: Path, *, fps: int = 30, bit: int | None = None,
              pad: int = 3, rounds: int = 2, resolution: str = "1080p") -> dict:
    """制片全流程：文件 → 方块帧 MP4，返回摘要供 CLI 打印 / GUI 显示。

    校验失败（源文件不存在 / 几何参数错）抛 ValueError；写片失败抛
    RuntimeError（含 ffmpeg stderr 尾巴）。不做打印与进程退出码，
    呈现方式由调用方（CLI 打印 / GUI 弹窗）决定。
    """
    startup_self_check()
    if not src.is_file():
        raise ValueError(f"源文件不存在：{src}")
    bit = bit if bit is not None else DEFAULT_BIT[resolution]
    canvas_w, canvas_h = RESOLUTIONS[resolution]
    try:
        geo = derive_geometry(canvas_w, canvas_h, bit, pad)
    except ValueError as e:
        raise ValueError(f"几何参数错误：{e}") from e

    plain = src.read_bytes()
    file_id = zlib.crc32(plain) & 0xFFFFFFFF  # fileId（纯内容 CRC32，§3）
    payload = gzip.compress(plain)
    frames = build_round(payload, file_id, src.name, len(plain), geo)
    write_tape(output, frames, geo, fps, rounds)
    return {
        "src": src,
        "output": output,
        "plain_size": len(plain),
        "payload_size": len(payload),
        "file_id": file_id,
        "geo": geo,
        "frame_count": len(frames),
        "rounds": rounds,
        "fps": fps,
        "duration": len(frames) * rounds / fps,
        "out_size": output.stat().st_size,
    }
