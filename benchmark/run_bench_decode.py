"""解码专项 benchmark（issue #30 C1+C2）：单帧解码耗时的优化前后对比。

测量口径
--------
纯离线：帧序列由 run_bench 的向量化渲染落盘 PNG 后读入内存（排除盘 I/O），
逐帧计时 decode_frame。几何推导沿用 run_bench.derive_geometry（sender.html
computeGeometry() 同式，dpr=1），矩阵 = v1 的窗口 × 输入 BIT + 4K 外推档
（C 类手段的目标场景：benchmark-v1 结论「4K 外推 67ms/帧 → 15fps 会翻转
瓶颈」）。

四个配置归因收益（_sample_grid 与几何检测两点独立优化）：

    legacy  全量几何检测（无缓存）+ 全图 boxFilter   = C1+C2 之前的旧实现
    c1      角标检测缓存    + 全图 boxFilter
    c2      全量几何检测    + 局部采样
    c1c2    角标检测缓存    + 局部采样               = 当前实现

legacy/c1 通过临时替换 receiver.pipeline._sample_grid 为 boxFilter 参照
实现获得（与 tests/test_pipeline_cache.py 的等价性参照同一函数）；所有
配置解码结果须与 legacy 逐位一致（DecodedFrame 相等），跑分同时是回归
验证。每帧计时跨 REPEATS 轮取 mean/median/p95。

用法：
    .venv/Scripts/python.exe benchmark/run_bench_decode.py          # 全量（约 2-4 分钟）
    .venv/Scripts/python.exe benchmark/run_bench_decode.py --quick  # 冒烟（秒级）
产出：benchmark/results/decode_results.json / .csv
"""

import csv
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "benchmark"))    # run_bench（几何推导 / 渲染复用）
sys.path.insert(0, str(ROOT / "tests"))        # fixture_encoder（封帧）

import fixture_encoder as fx  # noqa: E402
import run_bench  # noqa: E402
from receiver.pipeline import (  # noqa: E402
    DecodedFrame,
    FrameRejected,
    GeometryCache,
    decode_frame,
)
import receiver.pipeline as pipeline  # noqa: E402

# ---------- 参数矩阵 ----------

WINDOWS = [(800, 600), (1280, 800), (1920, 1080), (3840, 2160)]
BITS = [4, 8, 12]                # 输入 BIT（4K 档会被自动抬升，见 derive_geometry）
N_FRAMES = 60                    # 每几何渲染的不同内容帧数（缓存命中需同几何）
REPEATS = 3                      # 全体帧计时的重复轮数
RESULTS_DIR = Path(__file__).resolve().parent / "results"

CONFIGS = ["legacy", "c1", "c2", "c1c2"]
CONFIG_LABEL = {
    "legacy": "全量检测+全图boxFilter（旧实现）",
    "c1": "缓存+全图boxFilter",
    "c2": "全量检测+局部采样",
    "c1c2": "缓存+局部采样（当前实现）",
}


def sample_grid_boxfilter(bw: np.ndarray, geo, rect: tuple[int, int, int, int]) -> bytes:
    """旧实现参照：全图 boxFilter 后取格心（issue #30 改造前的 _sample_grid）。"""
    x0, y0, _, _ = rect
    k = min(5, geo.bit)
    if k % 2 == 0:
        k -= 1
    mean = cv2.boxFilter(bw, ddepth=-1, ksize=(k, k), borderType=cv2.BORDER_REPLICATE)
    centers_y = (y0 + (np.arange(geo.rows) + 0.5) * geo.bit).astype(int)
    centers_x = (x0 + (np.arange(geo.cols) + 0.5) * geo.bit).astype(int)
    window = mean[np.ix_(centers_y, centers_x)]
    bits = (window < 128).astype(np.uint8)
    return np.packbits(bits.flatten(), bitorder="big").tobytes()


def decode_with(config: str, img: np.ndarray, cache: GeometryCache | None) -> DecodedFrame:
    """按配置解码一帧：legacy/c1 临时走 boxFilter 采样；legacy/c2 不带缓存
    （cache 传了就会生效，C1 是 decode_frame 的参数而非配置开关）。"""
    if config in ("legacy", "c2"):
        cache = None
    if config in ("legacy", "c1"):
        real = pipeline._sample_grid
        pipeline._sample_grid = sample_grid_boxfilter
        try:
            return decode_frame(img, cache)
        finally:
            pipeline._sample_grid = real
    return decode_frame(img, cache)


def bench_geometry(geo: dict, frames_dir: Path, quick: bool) -> dict:
    """单几何：渲染 → 读入内存 → 四配置逐帧计时 → 一致性校验。"""
    n_frames = 8 if quick else N_FRAMES
    repeats = 1 if quick else REPEATS
    chunk = geo["chunk"]
    total = 4  # 封帧总数任意（>0 即可），渲染只取画面
    frames: list[np.ndarray] = []
    out_dir = frames_dir / "png"
    out_dir.mkdir(parents=True, exist_ok=True)
    for i in range(n_frames):
        payload = bytes((j * 131 + i * 37 + 11) % 256 for j in range(chunk))
        header = fx.signed_header(0xBEEF0000 + i, i % total, total, payload, chunk,
                                  geo["cols"], geo["rows"], geo["bit"], geo["pad"])
        p = out_dir / f"{i:06d}.png"
        run_bench.render_frame(header, payload, geo["bit"], geo["pad"],
                               geo["cols"], geo["rows"], p)
        img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
        assert img is not None
        frames.append(img)

    # 正确性前置：四配置解码结果与 legacy 逐位一致，任何拒绝即中止
    refs = [decode_with("legacy", img, None) for img in frames]
    for cfg in CONFIGS[1:]:
        cache = GeometryCache()
        for i, img in enumerate(frames):
            got = decode_with(cfg, img, cache)
            assert got == refs[i], f"{cfg} 第 {i} 帧解码结果与 legacy 不一致"

    run: dict = {"frames": n_frames, "px": int(frames[0].shape[0] * frames[0].shape[1])}
    for cfg in CONFIGS:
        cache = GeometryCache() if cfg in ("c1", "c1c2") else None
        decode_with(cfg, frames[0], cache)  # 预热（冷启动噪声不计入）
        samples_ms = []
        for _ in range(repeats):
            for img in frames:
                t0 = time.perf_counter()
                decode_with(cfg, img, cache)
                samples_ms.append((time.perf_counter() - t0) * 1000)
        samples_ms.sort()
        n = len(samples_ms)
        mean = statistics.fmean(samples_ms)
        run[cfg] = {
            "mean_ms": round(mean, 3),
            "median_ms": round(samples_ms[n // 2], 3),
            "p95_ms": round(samples_ms[int(n * 0.95) - 1], 3),
            "decode_fps": round(1000 / mean, 1),
        }
    run["speedup_c1c2"] = round(run["legacy"]["mean_ms"] / run["c1c2"]["mean_ms"], 2)
    return run


def parse_args(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--quick", action="store_true",
                    help="只跑 800x600 BIT4/8 × 8 帧 × 1 轮冒烟")
    return ap.parse_args(argv)


def main() -> int:
    args = parse_args()
    if args.quick:
        global WINDOWS, BITS
        WINDOWS, BITS = [(800, 600)], [4, 8]

    # 几何去重（自动抬升后同几何只测一次，先到先测）
    planned = []
    seen = set()
    for w, h in WINDOWS:
        for bit_in in BITS:
            geo = run_bench.derive_geometry(w, h, bit_in)
            key = (geo["cols"], geo["rows"], geo["bit"])
            if key in seen:
                continue
            seen.add(key)
            planned.append(geo)

    work_dir = RESULTS_DIR / "work" / "decode"
    runs = []
    for i, geo in enumerate(planned, 1):
        label = f"[{i}/{len(planned)}] {geo['w']}x{geo['h']} BIT={geo['bit']}"
        t0 = time.perf_counter()
        r = bench_geometry(geo, work_dir, args.quick)
        r.update({"window": f"{geo['w']}x{geo['h']}", "bit_input": geo["bit_input"],
                  "bit": geo["bit"], "cols": geo["cols"], "rows": geo["rows"],
                  "chunk_bytes": geo["chunk"]})
        runs.append(r)
        print(f"{label} 网格 {geo['cols']}x{geo['rows']} px={r['px']}: "
              + " ".join(f"{c}={r[c]['mean_ms']}ms" for c in CONFIGS)
              + f" → C1+C2 提速 {r['speedup_c1c2']}x（{time.perf_counter() - t0:.1f}s）")

    report = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "python": sys.version.split()[0],
            "quick": args.quick,
            "n_frames": 8 if args.quick else N_FRAMES,
            "repeats": 1 if args.quick else REPEATS,
            "pad": run_bench.PAD,
            "dpr": 1,
            "note": "mean/median/p95 为逐帧 decode_frame 耗时（帧图预读入内存，"
                    "不含盘 I/O）；legacy/c1 经替换 _sample_grid 为 boxFilter 参照实现",
        },
        "runs": runs,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "decode_results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with (RESULTS_DIR / "decode_results.csv").open("w", newline="", encoding="utf-8-sig") as f:
        wtr = csv.writer(f)
        wtr.writerow(["window", "bit_input", "bit", "cols", "rows", "chunk_bytes",
                      "px", "frames"] + [f"{c}_{k}" for c in CONFIGS
                                         for k in ("mean_ms", "median_ms", "p95_ms", "decode_fps")]
                      + ["speedup_c1c2"])
        for r in runs:
            row = [r["window"], r["bit_input"], r["bit"], r["cols"], r["rows"],
                   r["chunk_bytes"], r["px"], r["frames"]]
            for c in CONFIGS:
                row += [r[c]["mean_ms"], r[c]["median_ms"], r[c]["p95_ms"], r[c]["decode_fps"]]
            row.append(r["speedup_c1c2"])
            wtr.writerow(row)

    # 清理帧图，results 只留数据
    for p in (work_dir / "png").glob("*.png"):
        p.unlink()
    (work_dir / "png").rmdir()
    print(f"\n结果已写入 {RESULTS_DIR / 'decode_results.json'} 与 .csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
