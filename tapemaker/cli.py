"""制带工具 CLI（需求文档 §11.3）。

python -m tapemaker make <文件> -o out.mp4 [--fps N] [--bit N] [--pad N]
    [--rounds N] [--resolution 1080p|4k]

默认值待 calibrate（issue #40，模拟二压矩阵定标）回填；当前取工程上
可用的保守档位。calibrate 子命令为独立 issue，不在此实现。
"""

from __future__ import annotations

import argparse
import gzip
import time
import zlib
from pathlib import Path

from receiver.protocol import startup_self_check
from tapemaker.frames import DEFAULT_BIT, RESOLUTIONS, Geometry, build_round, derive_geometry
from tapemaker.render import TapeWriter, render_frame


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tapemaker",
        description="制带工具：文件编码为方块帧 MP4（视频信道场景，需求文档 §11）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    mk = sub.add_parser("make", help="制带：文件 → 方块帧 MP4")
    mk.add_argument("file", type=Path, help="待摆渡的源文件")
    mk.add_argument("-o", "--output", type=Path, required=True, help="输出 MP4 路径")
    mk.add_argument("--fps", type=int, default=30, help="视频帧率（默认 30，待定标）")
    mk.add_argument("--bit", type=int, default=None,
                    help="单块边长（物理像素，1–15）；缺省随分辨率档位（1080p=6 / 4k=15）")
    mk.add_argument("--pad", type=int, default=3, help="静默区宽（×BIT 像素，3–15，默认 3）")
    mk.add_argument("--rounds", type=int, default=2,
                    help="轮次重复：完整帧序列连播 N 轮（默认 2，ADR-0003）")
    mk.add_argument("--resolution", choices=sorted(RESOLUTIONS), default="1080p",
                    help="画布分辨率档（默认 1080p，4K 预留）")
    return parser


def cmd_make(args: argparse.Namespace) -> int:
    startup_self_check()  # §3 冻结：两端实现启动自检，不过则拒绝运行

    src: Path = args.file
    if not src.is_file():
        raise SystemExit(f"源文件不存在：{src}")
    bit = args.bit if args.bit is not None else DEFAULT_BIT[args.resolution]
    canvas_w, canvas_h = RESOLUTIONS[args.resolution]
    try:
        geo = derive_geometry(canvas_w, canvas_h, bit, args.pad)
    except ValueError as e:
        raise SystemExit(f"几何参数错误：{e}")

    t0 = time.monotonic()
    plain = src.read_bytes()
    file_id = zlib.crc32(plain) & 0xFFFFFFFF  # fileId（纯内容 CRC32，§3）
    payload = gzip.compress(plain)
    frames = build_round(payload, file_id, src.name, len(plain), geo)

    total_video_frames = len(frames) * args.rounds
    print(
        f"制带：{len(plain)} 字节 → gzip {len(payload)} 字节，fileId 0x{file_id:08X}\n"
        f"几何 {geo.canvas_w}×{geo.canvas_h} · COLS {geo.cols} × ROWS {geo.rows} · "
        f"BIT {geo.bit} · PAD {geo.pad} · CHUNK {geo.chunk_size} B\n"
        f"{len(frames)} 帧/轮 × {args.rounds} 轮 = {total_video_frames} 帧 · "
        f"{args.fps} fps · 时长 {total_video_frames / args.fps:.1f}s"
    )

    writer = TapeWriter(args.output, geo, args.fps)
    try:
        for round_no in range(1, args.rounds + 1):
            for frame in frames:
                writer.write(render_frame(frame, geo))
            print(f"第 {round_no}/{args.rounds} 轮渲染完成")
        writer.close()
    except Exception as e:
        raise writer.fail(f"制带失败：{e}") from e

    out_size = args.output.stat().st_size
    print(
        f"已出片：{args.output}（{out_size} 字节，{time.monotonic() - t0:.1f}s）\n"
        f"接收：上传视频平台后下载，python -m receiver receive --source video --video <下载文件>"
    )
    return 0


def main(argv=None) -> int:
    parser = _make_parser()
    args = parser.parse_args(argv)
    if args.cmd == "make":
        return cmd_make(args)
    parser.error(f"未知子命令：{args.cmd}")  # 不可达（required=True 已拦截）
    return 2
