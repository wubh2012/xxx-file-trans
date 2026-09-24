"""临时分析脚本：从 benchmark_results.json 提取 report.md 需要的数字。"""
import json
from pathlib import Path

data = json.load(open(Path(__file__).parent / "results" / "benchmark_results.json", encoding="utf-8"))
runs = data["runs"]

print("=== 几何与帧数（1MiB） ===")
for r in runs:
    if r["size_mib"] != 1:
        continue
    print(f"{r['window']:>10} bit_in={r['bit_input']:>2} eff={r['bit']:>2} "
          f"cols={r['cols']:>3} rows={r['rows']:>3} chunk={r['chunk_bytes']:>5} "
          f"帧={r['data_frames']:>4} 采样={r['sampled']}")

print()
print("=== 单帧解码耗时 vs 画布像素（1MiB 全量组） ===")
for r in runs:
    if r["size_mib"] != 1 or r["sampled"]:
        continue
    px = (r["cols"] + 2 * r["pad"]) * r["bit"] * (r["rows"] + 2 * r["pad"]) * r["bit"]
    print(f"{r['window']:>10} bit={r['bit']:>2} px={px:>9,} "
          f"per_frame={r['per_frame_ms']:>8}ms decode={r['decode_fps']:>6}fps")

print()
print("=== 单帧耗时随负载增长（同几何不同 size） ===")
for w, b in [("800x600", 4), ("1280x800", 8), ("1920x1080", 8)]:
    line = [f"{w} bit={b}:"]
    for r in runs:
        if r["window"] == w and r["bit"] == b:
            line.append(f"{r['size_mib']}MiB={r['per_frame_ms']}ms(s={int(r['sampled'])})")
    print("  ".join(line))

print()
print("=== 总耗时 @FPS10 与吞吐 ===")
for r in runs:
    d = r["fps10"]
    print(f"{r['size_mib']:>3}MiB {r['window']:>10} bit={r['bit']:>2} "
          f"帧={r['data_frames']:>6} total={d['total_s']:>8}s "
          f"瓶颈={d['bottleneck']:<8} 吞吐={d['throughput_mib_s']:.3f}MiB/s")

print()
print("=== FPS 对比（20MiB 两个代表几何） ===")
for r in runs:
    if r["size_mib"] == 20 and ((r["window"] == "1920x1080" and r["bit"] == 8)
                                or (r["window"] == "800x600" and r["bit"] == 12)):
        for fps in (5, 10, 20):
            d = r[f"fps{fps}"]
            print(f"{r['window']} bit={r['bit']} fps={fps} total={d['total_s']}s "
                  f"瓶颈={d['bottleneck']} 吞吐={d['throughput_mib_s']}MiB/s")

print()
print("=== 关系验证 ===")
# chunk ≈ (窗口/BIT)^2 数据位占比；帧数 ∝ BIT²/面积
r4 = next(r for r in runs if r["size_mib"] == 1 and r["window"] == "800x600" and r["bit"] == 4)
r8 = next(r for r in runs if r["size_mib"] == 1 and r["window"] == "800x600" and r["bit"] == 8)
print(f"BIT 4→8 帧数比 = {r8['data_frames'] / r4['data_frames']:.2f}（理论 ≈ 4）")
r4b = next(r for r in runs if r["size_mib"] == 1 and r["window"] == "1280x800" and r["bit"] == 8)
print(f"面积 800x600→1280x800（BIT=8）帧数比 = {r4['data_frames'] / r4b['data_frames']:.2f}（理论 ≈ {1280*800/(800*600):.2f}）")
# 最优 vs 最差（20MiB @fps10）
cands = [(r[f"fps10"]["total_s"], r) for r in runs if r["size_mib"] == 20]
best = min(cands, key=lambda t: t[0])
worst = max(cands, key=lambda t: t[0])
print(f"20MiB @FPS10 最优: {best[1]['window']} bit={best[1]['bit']} total={best[0]}s")
print(f"20MiB @FPS10 最差: {worst[1]['window']} bit={worst[1]['bit']} total={worst[0]}s  差 {worst[0]/best[0]:.1f} 倍")
# 解码速率下限
print(f"全矩阵解码速率范围: {min(r['decode_fps'] for r in runs)}–{max(r['decode_fps'] for r in runs)} fps（发送端 FPS 上限 30）")
# 每帧耗时 vs 画布像素（1MiB 全量组）线性拟合斜率
pts = []
for r in runs:
    if r["size_mib"] == 1 or r["sampled"]:
        continue
    px = (r["cols"] + 2 * r["pad"]) * r["bit"] * (r["rows"] + 2 * r["pad"]) * r["bit"]
    pts.append((px, r["per_frame_ms"]))
n = len(pts)
sx = sum(p[0] for p in pts); sy = sum(p[1] for p in pts)
sxx = sum(p[0] ** 2 for p in pts); sxy = sum(p[0] * p[1] for p in pts)
slope = (n * sxy - sx * sy) / (n * sxx - sx * sx)
inter = (sy - slope * sx) / n
print(f"单帧耗时 ≈ {inter:.2f}ms + {slope * 1e6:.2f}ns/px（1MiB 全量组 {n} 点线性拟合）")
