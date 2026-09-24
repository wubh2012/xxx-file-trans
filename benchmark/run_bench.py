"""参数矩阵 benchmark（issue：建立 benchmark 测试）。

测量口径
--------
发送端几何推导与 sender.html computeGeometry() 同式复刻（devicePixelRatio
按 1 计，即无显示缩放的 headless 基准）：

    cols = floor(W / BIT) - 2*PAD
    rows = floor(H / BIT) - 2*PAD
    CHUNK_SIZE = (cols*rows - 208) // 8     # 208 bit 帧头之外的整字节容量

帧序列由 tests/fixture_encoder.py 的帧头 / CRC / 元数据逻辑（协议独立实现）
封帧，渲染走 numpy 向量化版本（与 fixture render_png 同一画面约定，仅去掉
逐 bit Python 循环）；payload 为 os.urandom（不可压缩，gzip 后 ≈ 原长，
对应传输负载的 worst case）。

接收计时直接在进程内调用 receiver.run.run_receive（CLI / GUI 共用的接收
核心）：result.elapsed 口径为「首个数据帧落地 → 还原写盘完成」，即全速
解帧还原耗时（images 源不限速）。FPS 是发送端播放节奏参数，对解码耗时
无影响，故按信道模型折算：

    信道耗时(理想) = 导出帧数 / FPS          # 播放节奏主导
    接收总耗时     = max(信道耗时, 解帧耗时)  # 解码跟不上时接收端成瓶颈

帧数超出 MAX_FULL_FRAMES 的组合不做全量渲染（如 20MiB × BIT=12 ≈ 7 万帧），
改用 SAMPLE_DATA_FRAMES 帧采样实测单帧耗时后线性外推，结果以 sampled 标记。

用法：
    .venv/Scripts/python.exe benchmark/run_bench.py
产出：benchmark/results/benchmark_results.json / .csv
"""

import gzip
import hashlib
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))                 # receiver 包
sys.path.insert(0, str(ROOT / "tests"))       # fixture_encoder（协议独立实现）

import fixture_encoder as fx  # noqa: E402
from receiver.run import run_receive  # noqa: E402
from receiver.sources import iter_source  # noqa: E402

# ---------- 参数矩阵 ----------

PAD = 4                          # sender.html 默认 PAD
WINDOWS = [(800, 600), (1280, 800), (1920, 1080)]   # 小窗 / 中窗 / 全屏可用面积
BITS = [4, 8, 12]                # CSS px/块（dpr=1 时即物理 BIT）
FPS_LIST = [5, 10, 20]
SIZES_MIB = [1, 5, 10, 20]
MAX_FULL_FRAMES = 4000           # 数据帧数超过此值改采样外推
SAMPLE_DATA_FRAMES = 120         # 采样外推的数据帧数（含元数据帧节奏）

HEADER_BITS = 208

RESULTS_DIR = Path(__file__).resolve().parent / "results"


class NullReporter:
    """run_receive 的静默 reporter（ProgressReporter 同接口，不出进度条）。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def set_total(self, total, completed=None):
        pass

    def on_decoded(self, payload, is_new):
        pass

    def on_rejected(self, name, e):
        raise AssertionError(f"benchmark 帧不应被拒绝：{name}: {e}")

    def finish(self):
        pass


def derive_geometry(w: int, h: int, bit_in: int, pad: int = PAD) -> dict:
    """sender.html computeGeometry() 同式推导（dpr=1）。

    与发送端一致的两处修正：
      - BIT 自动抬升（COLS/ROWS 有帧头 1 字节上限 255）：bit = max(bit,
        ceil(W/(255+2*PAD)), ceil(H/(255+2*PAD)))，即大窗口下 BIT=4 会
        被抬到 8（1920 宽）等；
      - 冻结约束 (COLS×ROWS) mod 8 == 0（§1 数据区字节对齐）不满足时逐行
        下调 ROWS 至满足为止。发送端对这条无守卫（dpr=1 直跑会产出接收端
        整帧拒绝的非法几何），benchmark 不复刻该缺陷，差异仅等效于窗口
        高度少一行 BIT。
    """
    bit = max(bit_in,
              -(-w // (255 + 2 * pad)),
              -(-h // (255 + 2 * pad)))
    cols = w // bit - 2 * pad
    rows = h // bit - 2 * pad
    while (cols * rows) % 8 != 0:
        rows -= 1
    chunk = (cols * rows - HEADER_BITS) // 8
    assert 0 < cols <= 255 and 0 < rows <= 255, "COLS/ROWS 超出帧头 1 字节上限"
    assert chunk < 65536, "CHUNK_SIZE 超出帧头 2 字节上限"
    return {"w": w, "h": h, "bit_input": bit_in, "bit": bit, "pad": pad,
            "cols": cols, "rows": rows, "chunk": chunk}


def render_frame(header: bytes, payload: bytes, bit: int, pad: int,
                 cols: int, rows: int, path: Path) -> None:
    """一帧 → PNG（numpy 向量化，画面约定与 fixture_encoder.render_png 一致）。"""
    stream = np.unpackbits(np.frombuffer(header + payload, dtype=np.uint8))
    grid = np.ones((rows, cols), dtype=np.uint8) * 255   # 数据网格默认白（bit=0）
    flat = grid.ravel()
    dark = flat[: stream.size]
    dark[stream == 1] = 0                                # 黑块 = 1，行优先 MSB first
    grid = flat.reshape(rows, cols)

    cw = (cols + 2 * pad) * bit
    ch = (rows + 2 * pad) * bit
    canvas = np.zeros((ch, cw), dtype=np.uint8)           # 静默区黑
    oy = ox = pad * bit
    canvas[oy:oy + rows * bit, ox:ox + cols * bit] = \
        grid.repeat(bit, axis=0).repeat(bit, axis=1)
    m = 3 * bit                                           # 白色角标，边长 3×BIT
    canvas[oy - m:oy, ox - m:ox] = 255
    canvas[oy - m:oy, ox + cols * bit:ox + cols * bit + m] = 255
    canvas[oy + rows * bit:oy + rows * bit + m, ox - m:ox] = 255
    canvas[oy + rows * bit:oy + rows * bit + m, ox + cols * bit:ox + cols * bit + m] = 255
    cv2.imwrite(str(path), canvas)


def export_frames(payload: bytes, geo: dict, out_dir: Path, file_id: int,
                  filename: str, plain_size: int, n_data_frames: int | None = None):
    """整包封帧导出 PNG 序列（发送端节奏：每 100 数据帧前插一帧元数据帧）。

    返回 (数据帧数, 导出帧总数)。payload 须为 gzip 后的压缩字节流。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    chunk = geo["chunk"]
    total = n_data_frames if n_data_frames is not None else \
        (len(payload) + chunk - 1) // chunk
    seq = 0
    for frame_no in range(total):
        if frame_no % 100 == 0:
            mp = fx.metadata_payload(filename, plain_size, len(payload))
            header = fx.signed_header(file_id, fx.FRAME_NO_METADATA, total,
                                      mp, chunk, geo["cols"], geo["rows"],
                                      geo["bit"], geo["pad"])
            seq += 1
            render_frame(header, mp, geo["bit"], geo["pad"],
                         geo["cols"], geo["rows"], out_dir / f"{seq:06d}.png")
        data = payload[frame_no * chunk:(frame_no + 1) * chunk]
        header = fx.signed_header(file_id, frame_no, total, data, chunk,
                                  geo["cols"], geo["rows"], geo["bit"], geo["pad"])
        seq += 1
        render_frame(header, data, geo["bit"], geo["pad"],
                     geo["cols"], geo["rows"], out_dir / f"{seq:06d}.png")
    return total, seq


def run_one(size_mib: int | None, geo: dict, work_dir: Path,
            n_data_frames: int | None = None) -> dict:
    """一次实测：封帧导出 → run_receive 全速接收 → 计时与校验。

    n_data_frames 非 None 时为采样运行（payload 取对应长度），否则 size_mib
    必填，全量传输。
    """
    file_id = random.getrandbits(32)
    name = "bench.bin"
    if n_data_frames is not None:
        # 采样负载：令 gzip 后的压缩长度恰好落在 n_data_frames 个 chunk 内
        # （元数据 compressedSize 与 total×chunk 交叉校验要求 total =
        # ceil(compressedSize/chunk)，见 store 锁定校验）
        raw = os.urandom((n_data_frames - 1) * geo["chunk"] + geo["chunk"] // 2)
        export_dir = work_dir / "sample_frames"
    else:
        raw = os.urandom(size_mib * 1024 * 1024)
        export_dir = work_dir / "frames"
    comp = gzip.compress(raw)

    t0 = time.perf_counter()
    data_frames, exported = export_frames(comp, geo, export_dir, file_id, name,
                                          len(raw), n_data_frames)
    export_s = time.perf_counter() - t0

    out_dir = work_dir / "output"
    t0 = time.perf_counter()
    result = run_receive(iter_source("images", export_dir), out_dir,
                         reporter_factory=NullReporter, notify=None)
    wall_s = time.perf_counter() - t0

    assert result.code == 0, f"接收失败：{result.error}\n{result.incomplete}"
    assert result.sha256 == hashlib.sha256(raw).hexdigest(), \
        "还原 sha256 与源文件不一致"
    assert result.received == data_frames

    return {
        "data_frames": data_frames,
        "exported_frames": exported,
        "export_s": round(export_s, 3),
        "receive_elapsed_s": round(result.elapsed, 3),   # 首数据帧落地 → 写盘完成
        "receive_wall_s": round(wall_s, 3),
        "per_frame_ms": round(result.elapsed * 1000 / exported, 3),
        "decode_fps": round(exported / result.elapsed, 1),
        "file_id": f"{file_id:08X}",
        "size_bytes": len(raw),
    }


def parse_args(argv=None):
    """--quick 冒烟模式：1MiB × 单窗口 × BIT 4/8 × FPS 10，秒级验证脚本可用。"""
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--quick", action="store_true", help="只跑 1MiB/800x600/BIT 4,8 冒烟矩阵")
    return ap.parse_args(argv)


def main() -> int:
    global SIZES_MIB, WINDOWS, BITS, FPS_LIST
    args = parse_args()
    if args.quick:
        SIZES_MIB, WINDOWS, BITS, FPS_LIST = [1], [(800, 600)], [4, 8], [10]
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    runs = []
    total_combos = len(SIZES_MIB) * len(WINDOWS) * len(BITS)

    # 先过一遍矩阵，算出全量渲染的帧数规模，打印预估；
    # BIT 自动抬升后同尺寸出现相同生效几何的组合去重（先到先测）
    planned = []
    seen_geo = set()
    for size in SIZES_MIB:
        for (w, h) in WINDOWS:
            for bit in BITS:
                geo = derive_geometry(w, h, bit)
                key = (size, geo["cols"], geo["rows"], geo["bit"])
                if key in seen_geo:
                    continue
                seen_geo.add(key)
                data_frames = -(-size * 1024 * 1024 // geo["chunk"])
                sampled = data_frames > MAX_FULL_FRAMES
                planned.append((size, geo, data_frames, sampled))
    total_combos = len(planned)
    full_frames = sum(d for _, _, d, s in planned if not s) + \
        sum((d + 99) // 100 for _, _, d, s in planned if not s)
    print(f"去重后 {total_combos} 组合，其中全量实测 "
          f"{sum(1 for *_, s in planned if not s)} 组（约 {full_frames} 帧 PNG），"
          f"采样外推 {sum(1 for *_, s in planned if s)} 组（每组 "
          f"{SAMPLE_DATA_FRAMES + (SAMPLE_DATA_FRAMES + 99) // 100} 帧）")

    for i, (size, geo, data_frames, sampled) in enumerate(planned, 1):
        label = f"[{i}/{total_combos}] {size}MiB 窗口{geo['w']}x{geo['h']} BIT={geo['bit']}"
        work_dir = RESULTS_DIR / "work" / f"run_{size}mib_{geo['w']}x{geo['h']}_bit{geo['bit']}"
        t0 = time.perf_counter()
        if sampled:
            m = run_one(None, geo, work_dir, n_data_frames=SAMPLE_DATA_FRAMES)
            # 线性外推：单帧实测耗时 × 全量帧数（含元数据帧节奏）
            full_exported = data_frames + (data_frames + 99) // 100
            est_decode = m["per_frame_ms"] / 1000 * full_exported
            run = {
                "size_mib": size, "window": f"{geo['w']}x{geo['h']}",
                "bit_input": geo["bit_input"], "bit": geo["bit"],
                "pad": geo["pad"], "cols": geo["cols"],
                "rows": geo["rows"], "chunk_bytes": geo["chunk"],
                "sampled": True,
                "sample": m,
                "data_frames": data_frames,
                "exported_frames": full_exported,
                "export_s": None,
                "receive_elapsed_s": None,
                "est_decode_s": round(est_decode, 1),
                "decode_fps": round(full_exported / est_decode, 1),
                "per_frame_ms": m["per_frame_ms"],
                "sha_ok": True,
            }
        else:
            m = run_one(size, geo, work_dir)
            run = {
                "size_mib": size, "window": f"{geo['w']}x{geo['h']}",
                "bit_input": geo["bit_input"], "bit": geo["bit"],
                "pad": geo["pad"], "cols": geo["cols"],
                "rows": geo["rows"], "chunk_bytes": geo["chunk"],
                "sampled": False,
                "data_frames": m["data_frames"],
                "exported_frames": m["exported_frames"],
                "export_s": m["export_s"],
                "receive_elapsed_s": m["receive_elapsed_s"],
                "receive_wall_s": m["receive_wall_s"],
                "est_decode_s": m["receive_elapsed_s"],
                "decode_fps": m["decode_fps"],
                "per_frame_ms": m["per_frame_ms"],
                "sha_ok": True,
            }
        # 信道模型折算：信道耗时 = 导出帧数 / FPS；总耗时取瓶颈
        for fps in FPS_LIST:
            channel = run["exported_frames"] / fps
            run[f"fps{fps}"] = {
                "channel_s": round(channel, 1),
                "total_s": round(max(channel, run["est_decode_s"]), 1),
                "bottleneck": "receiver" if run["est_decode_s"] > channel else "playback",
                "throughput_mib_s": round(size / max(channel, run["est_decode_s"]), 3),
            }
        runs.append(run)
        print(f"{label} → {'采样' if sampled else '全量'} "
              f"数据帧 {run['data_frames']}，导出 {run['exported_frames']} 帧，"
              f"单帧 {run['per_frame_ms']}ms（解码 {run['decode_fps']} fps），"
              f"本轮 {time.perf_counter() - t0:.1f}s")
        # 清理本轮帧图，省磁盘（results 只留数据）
        for sub in ("frames", "sample_frames", "output"):
            d = work_dir / sub
            if d.is_dir():
                for p in d.iterdir():
                    p.unlink()
                d.rmdir()

    report = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "python": sys.version.split()[0],
            "pad": PAD,
            "dpr": 1,
            "windows": WINDOWS, "bits": BITS, "fps_list": FPS_LIST,
            "sizes_mib": SIZES_MIB,
            "max_full_frames": MAX_FULL_FRAMES,
            "sample_data_frames": SAMPLE_DATA_FRAMES,
            "metric_note": "receive_elapsed_s = receiver.run.run_receive 的 elapsed"
                           "（首数据帧落地→还原写盘完成，全速）；total_s = max(导出帧数/FPS, 解帧耗时)",
        },
        "runs": runs,
    }
    (RESULTS_DIR / "benchmark_results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # CSV：长表，每 (组合 × FPS) 一行
    import csv
    with (RESULTS_DIR / "benchmark_results.csv").open("w", newline="", encoding="utf-8-sig") as f:
        wtr = csv.writer(f)
        wtr.writerow(["size_mib", "window", "bit_input", "bit", "cols", "rows",
                      "chunk_bytes", "data_frames", "exported_frames", "sampled",
                      "per_frame_ms", "decode_fps", "receive_elapsed_s",
                      "fps", "channel_s", "est_decode_s", "total_s",
                      "bottleneck", "throughput_mib_s"])
        for r in runs:
            for fps in FPS_LIST:
                d = r[f"fps{fps}"]
                wtr.writerow([r["size_mib"], r["window"], r["bit_input"],
                              r["bit"], r["cols"],
                              r["rows"], r["chunk_bytes"], r["data_frames"],
                              r["exported_frames"], r["sampled"],
                              r["per_frame_ms"], r["decode_fps"],
                              r["receive_elapsed_s"], fps, d["channel_s"],
                              r["est_decode_s"], d["total_s"],
                              d["bottleneck"], d["throughput_mib_s"]])
    print(f"\n结果已写入 {RESULTS_DIR / 'benchmark_results.json'} 与 .csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
