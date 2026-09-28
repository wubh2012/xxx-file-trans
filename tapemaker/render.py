"""帧渲染与带写入（issue #38）。

渲染与 sender.html / ADR-0001 同一几何（冻结）：静默区黑色、数据网格
默认白、置位（黑块 = 1）涂黑、四角白色角标（边长 3×BIT，内角贴网格
外角）。numpy 生成方块阵列 → rawvideo 管道直喂 ffmpeg，零中间帧文件。

编码策略依据 ADR-0003：all-intra（`-g 1`，每个视频帧独立编码，重复帧
之间不共享预测，轮次重复的冗余才真正生效）。
"""

from __future__ import annotations

import shutil
import subprocess

import numpy as np

from tapemaker.frames import Frame, Geometry

CORNER_MULTIPLE = 3  # 角标边长 = 3×BIT（§6 冻结）


def render_frame(frame: Frame, geo: Geometry) -> np.ndarray:
    """一帧 → (canvas_h, canvas_w) uint8 灰度位图。黑块 = 1，行优先、
    字节内 MSB first（与 sender.html / fixture_encoder 逐位一致）。"""
    bit, pad = geo.bit, geo.pad
    grid_bytes = np.frombuffer(frame.header + frame.data, dtype=np.uint8)
    cell_count = geo.cols * geo.rows
    if len(grid_bytes) * 8 < cell_count:
        raise ValueError("帧字节不足网格容量（内部错误）")
    bits = np.unpackbits(grid_bytes, count=cell_count)  # MSB first
    grid = np.where(bits == 1, np.uint8(0), np.uint8(255)).reshape(geo.rows, geo.cols)
    bitmap = grid.repeat(bit, axis=0).repeat(bit, axis=1)

    canvas = np.zeros((geo.canvas_h, geo.canvas_w), dtype=np.uint8)  # 静默区黑
    o = pad * bit
    gh, gw = geo.rows * bit, geo.cols * bit
    if o + gh > geo.canvas_h or o + gw > geo.canvas_w:
        raise ValueError("数据网格超出画布（内部错误）")
    canvas[o : o + gh, o : o + gw] = bitmap
    m = CORNER_MULTIPLE * bit  # 角标：白色实心方块，内角贴网格外角（伸入静默区）
    canvas[o - m : o, o - m : o] = 255
    canvas[o - m : o, o + gw : o + gw + m] = 255
    canvas[o + gh : o + gh + m, o - m : o] = 255
    canvas[o + gh : o + gh + m, o + gw : o + gw + m] = 255
    return canvas


class TapeWriter:
    """rawvideo 管道直喂 ffmpeg（零中间帧文件）。

    ffmpeg 用系统安装版本，缺失时报错并提示安装方式（需求 §11.2）。
    all-intra 编码依据 ADR-0003 决策 1；CRF 取低值——黑白方块内容熵极低，
    体积代价可接受，制带侧宁多花码率保角标边缘锐利（平台二压才是主要损失源）。
    """

    def __init__(self, out_path, geo: Geometry, fps: int) -> None:
        exe = shutil.which("ffmpeg")
        if not exe:
            raise RuntimeError(
                "未找到 ffmpeg：制带依赖系统安装的 ffmpeg。"
                "Windows 可 winget install Gyan.FFmpeg，或从 https://ffmpeg.org/download.html 安装后重试"
            )
        cmd = [
            exe, "-y",
            "-f", "rawvideo", "-pix_fmt", "gray",
            "-s", f"{geo.canvas_w}x{geo.canvas_h}", "-r", str(fps),
            "-i", "-",
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "12",
            "-g", "1",  # all-intra：每个视频帧都是 I 帧（ADR-0003）
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(out_path),
        ]
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE
        )

    def write(self, img: np.ndarray) -> None:
        self._proc.stdin.write(img.tobytes())

    def close(self) -> None:
        """收尾：关 stdin 等 ffmpeg 出片；失败时带出 ffmpeg 的报错尾巴。"""
        self._proc.stdin.close()
        _, stderr = self._proc.communicate()
        if self._proc.returncode != 0:
            raise RuntimeError(self._ffmpeg_error(stderr))

    def fail(self, message: str) -> RuntimeError:
        """写帧中途出错的收口：杀掉 ffmpeg，报错带出其 stderr 尾巴。"""
        if self._proc.poll() is None:
            self._proc.kill()
        _, stderr = self._proc.communicate()
        detail = f"\n{self._ffmpeg_error(stderr)}" if self._proc.returncode not in (0, None) else ""
        return RuntimeError(f"{message}{detail}")

    @staticmethod
    def _ffmpeg_error(stderr: bytes) -> str:
        tail = stderr.decode(errors="replace").strip().splitlines()[-5:]
        return "ffmpeg 报错（末 5 行）：\n" + "\n".join(tail)
