"""固定种子丢帧仿真；传输时间为帧数/FPS，不是桌面实测。"""

import argparse
import gzip
import json
import random
import statistics
import sys
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from receiver.fec import gf_mul
from receiver.fountain import repair_payload
from receiver.metadata import FileMetadata
from receiver.pipeline import DecodedFrame, FrameHeader
from receiver.protocol import FLAGS_FEC, FLAGS_FOUNTAIN, FLAGS_GZIP, FLAGS_REPAIR
from receiver.store import FrameStore


def source_sequence(total, mode, ratio="32:2", depth=64):
    """与 sender.buildPlaybackSequence 对齐；tail 为改造前的喷泉码调度。"""
    groups = (total + 31) // 32
    stride, burst = map(int, ratio.split(":"))
    sequence = []
    data_count = repair_count = 0
    for base in range(0, groups, depth):
        for slot in range(32 if mode == "fountain" else 34):
            for group in range(base, min(groups, base + depth)):
                if slot < 32:
                    n = group * 32 + slot
                    if n >= total:
                        continue
                    if data_count % 100 == 0:
                        sequence.append(-1)
                    data_count += 1
                    sequence.append(n)
                    if mode == "fountain" and data_count % stride == 0:
                        for _ in range(burst):
                            sequence.append(-2 - total - repair_count)
                            repair_count += 1
                else:
                    relative = (group * 2 + slot - 32 if mode == "fec"
                                else (slot - 32) * groups + group)
                    sequence.append(-2 - total - relative)
    if mode == "fountain" and data_count % stride:
        for _ in range(burst):
            sequence.append(-2 - total - repair_count)
            repair_count += 1
    return sequence


def simulate(mode, scenario, seed, total_target=256, chunk=1024, fps=30, ratio="32:2"):
    raw = random.Random(20261003).randbytes(total_target * chunk - 100)
    compressed = gzip.compress(raw, mtime=0)
    parts = [compressed[i:i + chunk] for i in range(0, len(compressed), chunk)]
    total = len(parts)
    groups = (total + 31) // 32
    file_id = zlib.crc32(raw) & 0xFFFFFFFF
    flags = FLAGS_GZIP | (FLAGS_FOUNTAIN if mode != "fec" else 0)
    def frame(n, payload, extra=0):
        return DecodedFrame(FrameHeader(file_id, n, total, len(payload), chunk,
            120, 70, 4, 3, flags | extra), payload)
    metadata = frame(0xFFFFFF, b"")
    metadata.metadata = FileMetadata(1, len(raw), len(compressed), "benchmark.bin")
    parities = {}
    if mode == "fec":
        for group in range(groups):
            group_parts = parts[group * 32:(group + 1) * 32]
            for p in range(2):
                out = bytearray(chunk)
                for i, part in enumerate(group_parts):
                    for j, value in enumerate(part):
                        out[j] ^= value if p == 0 else gf_mul(i + 1, value)
                parities[group, p] = bytes(out)

    store = FrameStore()
    rng = random.Random(seed)
    sent = 0
    cpu = 0.0
    milestones = {}
    # 以相同发送时隙上的损失规则比较；上限为 12 个旧协议周期。
    budget = 12 * (total + groups * 2 + (total + 99) // 100)
    def deliver(f):
        nonlocal sent, cpu
        sent += 1
        lost = (scenario == "random10" and rng.random() < .1
                or scenario == "random25" and rng.random() < .25
                or scenario == "burst20" and (sent - 1) % 100 < 20
                or scenario == "fixed_missing" and f.header.frame_no < total
                    and f.header.frame_no % 32 in {3, 7, 11})
        if not lost:
            started = time.perf_counter()
            store.add(f)
            cpu += time.perf_counter() - started
        for percent in (90, 95, 99):
            if percent not in milestones and store.received_count() >= percent / 100 * total:
                milestones[percent] = sent
        return store.is_complete() or sent >= budget

    done = False
    serial = 0
    round_index = 0
    first = source_sequence(total, mode, ratio)
    next_serial = ((sum(n < -1 for n in first) + groups - 1) // groups)
    while not done:
        if mode == "fec" or round_index == 0:
            for item in first:
                if item == -1:
                    f = metadata
                elif item >= 0:
                    f = frame(item, parts[item])
                else:
                    n = -item - 2
                    if mode == "fec":
                        group, p = divmod(n - total, 2)
                        f = frame(n, parities[group, p], FLAGS_FEC)
                    else:
                        group = (n - total) % groups
                        f = frame(n, repair_payload(parts[group * 32:(group + 1) * 32],
                                                   file_id, n, chunk), FLAGS_REPAIR)
                if deliver(f):
                    done = True
                    break
            serial = next_serial
        else:
            repair_count = 0
            for s in range(serial, serial + 2):
                for group in range(groups):
                    if repair_count % 100 == 0 and deliver(metadata):
                        done = True
                        break
                    repair_count += 1
                    n = total + s * groups + group
                    f = frame(n, repair_payload(parts[group * 32:(group + 1) * 32],
                              file_id, n, chunk), FLAGS_REPAIR)
                    if deliver(f):
                        done = True
                        break
                if done:
                    break
            serial += 2
        round_index += 1
    complete = store.is_complete()
    if complete:
        assert gzip.decompress(store.assemble()) == raw
    return {"mode": mode, "scenario": scenario, "seed": seed, "frames_sent": sent,
            "repair_ratio": ratio if mode == "fountain" else None,
            "complete": complete, "received": store.received_count(), "total": total,
            "nominal_seconds": round(sent / fps, 3),
            **{f"tail{p}_seconds": None if p not in milestones or not complete
               else round((sent - milestones[p]) / fps, 3) for p in (90, 95, 99)},
            "store_cpu_ms": round(cpu * 1000, 3)}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", type=int, default=20)
    ap.add_argument("--out", type=Path, default=ROOT / "benchmark/results/fountain-interspersed-simulation.json")
    args = ap.parse_args()
    runs = [simulate(mode, scenario, seed, ratio=ratio) for scenario in
            ("clean", "random10", "random25", "burst20", "fixed_missing")
            for seed in range(args.seeds) for mode, ratio in
            [("fec", "32:2"), ("tail", "32:2"), ("fountain", "32:1"),
             ("fountain", "32:2"), ("fountain", "16:1"), ("fountain", "8:1")]]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"kind": "loss simulation, not desktop measurement",
        "fps": 30, "runs": runs}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("scenario mode ratio complete median_seconds median_store_ms")
    for scenario in dict.fromkeys(r["scenario"] for r in runs):
        for mode, ratio in dict.fromkeys((r["mode"], r["repair_ratio"]) for r in runs):
            subset = [r for r in runs if r["scenario"] == scenario and r["mode"] == mode
                      and r["repair_ratio"] == ratio]
            good = [r for r in subset if r["complete"]]
            seconds = statistics.median(r["nominal_seconds"] for r in good) if good else None
            print(scenario, mode, ratio, f"{len(good)}/{len(subset)}", seconds,
                  round(statistics.median(r["store_cpu_ms"] for r in subset), 2))


if __name__ == "__main__":
    main()
