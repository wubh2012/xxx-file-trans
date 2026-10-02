"""制片工具 CLI（需求文档 §11.3）。

python -m tapemaker make <文件> -o out.mp4 [--fps N] [--bit N] [--pad N]
    [--rounds N] [--resolution 1080p|4k]
python -m tapemaker calibrate <文件> -o <目录> [--crf 档位] [--bit 档位]
    [--resolution 档位] [--strategy 档位] [--fps N] [--rounds N]

calibrate（issue #40，§11.4.2 模拟二压矩阵）：ffmpeg 重编码（CRF 档位）
近似平台二压，扫 CRF × BIT × 分辨率 × I 帧策略矩阵，输出各组合 CRC
存活率表 + 推荐默认参数；B站二压策略变化后可重跑。
"""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
import subprocess
import time
import zlib
from pathlib import Path

import cv2

from receiver.pipeline import FrameRejected, GeometryCache, decode_frame
from receiver.protocol import startup_self_check
from tapemaker.frames import DEFAULT_BIT, RESOLUTIONS, build_round, derive_geometry
from tapemaker.make import make_tape, write_tape

MAKE_CRF = 12  # 制片侧码率档：低 CRF 保角标边缘锐利，二压才是主要损失源


def _int_list(text: str) -> list[int]:
    """「18,23,28」→ [18, 23, 28]（升序去重）。"""
    try:
        values = sorted({int(x) for x in text.split(",") if x.strip()})
    except ValueError:
        raise argparse.ArgumentTypeError(f"应为逗号分隔的整数：{text!r}")
    if not values:
        raise argparse.ArgumentTypeError("至少一个值")
    return values


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tapemaker",
        description="制片工具：文件编码为方块帧 MP4（视频信道场景，需求文档 §11）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    mk = sub.add_parser("make", help="制片：文件 → 方块帧 MP4")
    mk.add_argument("file", type=Path, help="待摆渡的源文件")
    mk.add_argument("-o", "--output", type=Path, required=True, help="输出 MP4 路径")
    mk.add_argument("--fps", type=int, default=30, help="视频帧率（默认 30，待定标）")
    mk.add_argument("--bit", type=int, default=None,
                    help="单块边长（物理像素，1–15）；缺省随分辨率档位（1080p=8 / 4k=15）")
    mk.add_argument("--pad", type=int, default=3, help="静默区宽（×BIT 像素，3–15，默认 3）")
    mk.add_argument("--rounds", type=int, default=2,
                    help="轮次重复：完整帧序列连播 N 轮（默认 2，ADR-0003）")
    mk.add_argument("--resolution", choices=sorted(RESOLUTIONS), default="1080p",
                    help="画布分辨率档（默认 1080p，4K 预留）")

    cal = sub.add_parser("calibrate", help="模拟二压矩阵定标：CRC 存活率表 + 推荐参数")
    cal.add_argument("file", type=Path, help="定标用样本文件（贴近真实负载）")
    cal.add_argument("-o", "--output", type=Path, required=True, help="输出目录（存各组合样片）")
    cal.add_argument("--crf", type=_int_list, default=[18, 23, 28],
                     help="模拟二压的 CRF 档位（逗号分隔，默认 18,23,28；值越大压得越狠）")
    cal.add_argument("--bit", type=_int_list, default=None,
                     help="BIT 档位（逗号分隔）；缺省按分辨率档取 [默认值, 默认值-2]")
    cal.add_argument("--resolution", default="1080p",
                     help="分辨率档位（逗号分隔，取值 1080p/4k，默认 1080p）")
    cal.add_argument("--strategy", default="allintra",
                     help="I 帧策略档位（逗号分隔，取值 allintra/gop，默认 allintra；ADR-0003）")
    cal.add_argument("--fps", type=int, default=30, help="视频帧率（默认 30）")
    cal.add_argument("--pad", type=int, default=3, help="静默区宽（默认 3）")
    cal.add_argument("--rounds", type=int, default=2, help="轮次重复（默认 2）")
    return parser


def _write_tape(out_path: Path, frames: list[Frame], geo: Geometry, fps: int,
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


def cmd_make(args: argparse.Namespace) -> int:
    src: Path = args.file
    t0 = time.monotonic()
    try:
        summary = make_tape(src, args.output, fps=args.fps, bit=args.bit,
                            pad=args.pad, rounds=args.rounds,
                            resolution=args.resolution)
    except ValueError as e:  # 源文件 / 几何校验失败
        raise SystemExit(str(e)) from e
    geo = summary["geo"]
    total_video_frames = summary["frame_count"] * args.rounds
    print(
        f"制片：{summary['plain_size']} 字节 → gzip {summary['payload_size']} 字节，"
        f"fileId 0x{summary['file_id']:08X}\n"
        f"几何 {geo.canvas_w}×{geo.canvas_h} · COLS {geo.cols} × ROWS {geo.rows} · "
        f"BIT {geo.bit} · PAD {geo.pad} · CHUNK {geo.chunk_size} B\n"
        f"{summary['frame_count']} 帧/轮 × {args.rounds} 轮 = {total_video_frames} 帧 · "
        f"{args.fps} fps · 时长 {summary['duration']:.1f}s"
    )
    print(
        f"已出片：{summary['output']}（{summary['out_size']} 字节，"
        f"{time.monotonic() - t0:.1f}s）\n"
        f"接收：上传视频平台后下载，python -m receiver receive --source video --video <下载文件>"
    )
    return 0


# ---------- calibrate：模拟二压矩阵（issue #40） ----------

def _reencode(master: Path, out_path: Path, crf: int) -> None:
    """ffmpeg 重编码近似平台二压（有损转码；平台实际参数不可知，按 CRF
    档位扫——档位即二压强度假设）。"""
    exe = shutil.which("ffmpeg")
    proc = subprocess.run(
        [exe, "-y", "-i", str(master), "-c:v", "libx264", "-preset", "medium",
         "-crf", str(crf), "-pix_fmt", "yuv420p", str(out_path)],
        capture_output=True,
    )
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace").strip().splitlines()[-3:]
        raise RuntimeError(f"模拟二压失败（crf {crf}）：\n" + "\n".join(tail))


def _survival(video: Path, total_data_frames: int) -> dict:
    """解帧逐帧过 receiver 流水线（几何自举 + CRC 仲裁），统计唯一数据帧
    存活率：≥1 份副本通过 CRC 即视为存活（轮次重复的冗余语义）。"""
    cache = GeometryCache()
    ok_frames: set[int] = set()
    crc_fail = 0
    decoded = 0
    cap = cv2.VideoCapture(str(video))
    try:
        while True:
            ok, img = cap.read()
            if not ok:
                break
            decoded += 1
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            try:
                frame = decode_frame(gray, cache)
            except FrameRejected:
                crc_fail += 1
                continue
            if frame.header.frame_no < total_data_frames:
                ok_frames.add(frame.header.frame_no)
    finally:
        cap.release()
    return {
        "survival": len(ok_frames) / total_data_frames if total_data_frames else 0.0,
        "uniqueOk": len(ok_frames),
        "total": total_data_frames,
        "crcRejected": crc_fail,
        "decodedFrames": decoded,
    }


def _combo_tag(resolution: str, bit: int, strategy: str) -> str:
    return f"{resolution}_bit{bit}_{strategy}"


def cmd_calibrate(args: argparse.Namespace) -> int:
    startup_self_check()

    src: Path = args.file
    if not src.is_file():
        raise SystemExit(f"源文件不存在：{src}")
    resolutions = [r.strip() for r in args.resolution.split(",") if r.strip()]
    for r in resolutions:
        if r not in RESOLUTIONS:
            raise SystemExit(f"未知分辨率档：{r}（可选 {'/'.join(sorted(RESOLUTIONS))}）")
    strategies = [s.strip() for s in args.strategy.split(",") if s.strip()]
    for s in strategies:
        if s not in ("allintra", "gop"):
            raise SystemExit(f"未知 I 帧策略：{s}（可选 allintra/gop；语义见 ADR-0003）")
    bits = args.bit
    if bits is None:
        bits = sorted({b for r in resolutions for b in (DEFAULT_BIT[r], DEFAULT_BIT[r] - 2)})

    plain = src.read_bytes()
    payload = gzip.compress(plain)
    file_id = zlib.crc32(plain) & 0xFFFFFFFF
    out_dir: Path = args.output
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    for resolution in resolutions:
        canvas_w, canvas_h = RESOLUTIONS[resolution]
        for bit in bits:
            try:
                geo = derive_geometry(canvas_w, canvas_h, bit, args.pad)
            except ValueError as e:
                print(f"跳过 {resolution} BIT {bit}：{e}")
                continue
            frames = build_round(payload, file_id, src.name, len(plain), geo)
            total = (len(payload) + geo.chunk_size - 1) // geo.chunk_size  # 数据帧总数
            for strategy in strategies:
                round_len = len(frames)
                gop = 1 if strategy == "allintra" else round_len  # 每重复单元一 I 帧（ADR-0003 备选 3）
                tag = _combo_tag(resolution, bit, strategy)
                master = out_dir / f"{tag}.mp4"
                t0 = time.monotonic()
                write_tape(master, frames, geo, args.fps, args.rounds, gop=gop)
                for crf in args.crf:
                    reenc = out_dir / f"{tag}_crf{crf}.mp4"
                    _reencode(master, reenc, crf)
                    stats = _survival(reenc, total)
                    rows.append({
                        "resolution": resolution, "bit": bit, "strategy": strategy,
                        "crf": crf, "sizeBytes": reenc.stat().st_size,
                        **stats,
                    })
                    print(
                        f"[{tag}] crf {crf}: 存活 {stats['uniqueOk']}/{stats['total']}"
                        f"（{stats['survival']:.1%}）· CRC 拒 {stats['crcRejected']}"
                        f" · {reenc.stat().st_size} B"
                    )
                print(f"[{tag}] 完成（{time.monotonic() - t0:.1f}s）")

    # 推荐口径（回填 make 默认值）：在最严 CRF 档仍 100% 存活的组合里，
    # 选最小 BIT（单帧容量最大 → 片最短）；并列选样片更小的策略档。
    # N / fps 为本次矩阵的运行参数——存活率在该 N 下测得，如实推荐；
    # 降 N 或变 fps 是否仍 100% 存活需重跑定标（spec #40：回填 BIT 下限、N、fps、I 帧策略）。
    harshest = max(args.crf)
    survivors = [r for r in rows if r["crf"] == harshest and r["survival"] >= 1.0]
    recommended = None
    if survivors:
        best = min(survivors, key=lambda r: (r["bit"], r["sizeBytes"]))
        recommended = {"resolution": best["resolution"], "bit": best["bit"],
                       "strategy": best["strategy"], "rounds": args.rounds,
                       "fps": args.fps, "crfSurvived": harshest}

    print("\n===== 定标摘要 =====")
    if recommended:
        print(
            f"推荐默认：分辨率 {recommended['resolution']} · BIT 下限 {recommended['bit']}"
            f" · I 帧策略 {recommended['strategy']} · N {recommended['rounds']}"
            f" · fps {recommended['fps']}（最严 crf {harshest} 下 100% 存活）"
        )
        print(
            f"回填 make：--bit {recommended['bit']} --rounds {recommended['rounds']}"
            f" --fps {recommended['fps']}（N/fps 为本次运行参数，降 N / 变 fps 需重跑；"
            f"I 帧策略经 TapeWriter(gop=…) 生效）"
        )
    else:
        print(f"最严 crf {harshest} 下无 100% 存活组合：放宽 CRF 档位或加大轮次重复后重跑")
    print(json.dumps({"recommended": recommended, "rows": rows}, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    parser = _make_parser()
    args = parser.parse_args(argv)
    if args.cmd == "make":
        return cmd_make(args)
    if args.cmd == "calibrate":
        return cmd_calibrate(args)
    parser.error(f"未知子命令：{args.cmd}")  # 不可达（required=True 已拦截）
    return 2
