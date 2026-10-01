"""汇总 run_bench_live.py --trace 生成的 JSONL 接收日志。"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((len(ordered) - 1) * fraction)
    return ordered[index]


def duration_line(label: str, values: list[float]) -> str:
    if not values:
        return f"{label}: 无数据"
    return (f"{label}: 中位 {statistics.median(values):.3f} ms, "
            f"p95 {percentile(values, .95):.3f} ms, 最大 {max(values):.3f} ms")


def analyze(path: Path) -> str:
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
              if line.strip()]
    if not events:
        raise ValueError("日志为空")

    metadata = next((e for e in events if e.get("event") == "run_metadata"), {})
    frames = [e for e in events if e.get("event") == "frame"]
    data = [e for e in frames if e.get("frame_kind") == "data"]
    new_data = [e for e in data if e.get("is_new")]
    rejects = [e for e in events if e.get("event") == "frame_rejected"]
    milestones = [e for e in events if e.get("event") == "progress_milestone"]
    stalls = [e for e in events if e.get("event") == "progress_stall"]
    idle = [e for e in events if e.get("event") == "stream_idle"]
    done = next((e for e in reversed(events) if e.get("event") == "receive_done"), None)
    incomplete = next((e for e in reversed(events)
                       if e.get("event") == "receive_incomplete"), None)
    total = metadata.get("total_frames")
    interleaved = metadata.get("playback_order") == "interleaved"

    wraps = 0
    rounds = [{"start": None, "end": None, "new": 0, "events": 0}]
    previous = None
    for event in data:
        frame_no = event.get("frame_no")
        if not interleaved and isinstance(frame_no, int) and isinstance(previous, int) and total:
            if previous - frame_no > total / 2:
                wraps += 1
                rounds.append({"start": None, "end": None, "new": 0, "events": 0})
        round_stat = rounds[-1]
        if round_stat["start"] is None:
            round_stat["start"] = event.get("t_rel_s")
        round_stat["end"] = event.get("t_rel_s")
        round_stat["events"] += 1
        round_stat["new"] += bool(event.get("is_new"))
        if isinstance(frame_no, int):
            previous = frame_no

    output = [
        f"日志: {path}",
        (f"输入: {metadata.get('input_bytes', '?')} bytes, "
         f"FPS {metadata.get('fps', '?')}, BIT {metadata.get('bit', '?')}, "
         f"{metadata.get('cols', '?')}×{metadata.get('rows', '?')}, "
         f"数据帧 {total if total is not None else '?'}"),
        (f"收到数据帧: 新帧 {len(new_data)}, 重复 {len(data) - len(new_data)}, "
         f"FEC {sum(e.get('frame_kind') == 'fec' for e in frames)}, "
         f"元数据 {sum(e.get('frame_kind') == 'metadata' for e in frames)}, "
         + ("交错播放：帧号回退不用于统计轮次" if interleaved else
            f"观察到播放轮次切换 {wraps}")),
        f"拒帧 {len(rejects)} ({dict(Counter(e.get('reason', '?') for e in rejects))})",
        duration_line("帧间隔", [e["gap_ms"] for e in frames
                                  if isinstance(e.get("gap_ms"), (int, float))]),
        duration_line("解码耗时", [e["decode_ms"] for e in frames
                                    if isinstance(e.get("decode_ms"), (int, float))]),
        duration_line("存储耗时", [e["store_ms"] for e in frames
                                    if isinstance(e.get("store_ms"), (int, float))]),
        f"采集空等事件 {len(idle)}; 队列丢帧 {done.get('queue_dropped', '?') if done else '?'}",
        ("接收区间新收数据帧: " if interleaved else "每轮新收数据帧: ") + "; ".join(
            f"{'区间' if interleaved else '第'} {i} {'新帧' if interleaved else '轮'} {stat['new']} 帧, "
            f"t={stat['start'] if stat['start'] is not None else '?'}–"
            f"{stat['end'] if stat['end'] is not None else '?'}s"
            for i, stat in enumerate(rounds, start=1)),
    ]

    first_99 = next((e for e in milestones if e.get("label") == "99%"), None)
    last_data = data[-1] if data else None
    if first_99:
        missing = first_99.get("missing_frame_numbers", [])
        output.append(
            f"99%检查点: t={first_99.get('t_rel_s', '?')}s, "
            f"缺 {first_99.get('missing_count', '?')} 帧 {missing}"
        )
        if last_data and isinstance(last_data.get("t_rel_s"), (int, float)):
            output.append(
                f"99%检查点到最后一个数据帧: "
                f"{last_data['t_rel_s'] - first_99['t_rel_s']:.3f}s"
            )
    for event in stalls:
        output.append(
            f"长尾心跳: t={event.get('t_rel_s', '?')}s, "
            f"缺 {event.get('missing_count', '?')} 帧 "
            f"{event.get('missing_frame_numbers', [])}"
        )
    for event in milestones:
        if event.get("label") == "100%" and event.get("missing_count", 0):
            output.append(
                f"提示: 100%显示时仍缺 {event['missing_count']} 帧；"
                "该旧日志使用四舍五入进度标签"
            )

    if done:
        output.append(
            f"完成: t={done.get('t_rel_s', '?')}s, "
            f"{done.get('received', '?')}/{done.get('total', '?')}, "
            f"code={done.get('code', '?')}"
        )
        if first_99 and isinstance(done.get("t_rel_s"), (int, float)):
            output.append(f"99%检查点到接收完成: {done['t_rel_s'] - first_99['t_rel_s']:.3f}s")
    elif incomplete:
        output.append(
            f"未完成: t={incomplete.get('t_rel_s', '?')}s, "
            f"{incomplete.get('received', '?')}/{incomplete.get('total', '?')}, "
            f"缺帧 {incomplete.get('missing_frame_numbers', [])}"
        )
    else:
        output.append("状态: 日志没有 receive_done / receive_incomplete 结尾事件")

    stages = [e for e in events if e.get("event") == "restore_stage"
              and isinstance(e.get("duration_ms"), (int, float))]
    if stages:
        output.append("还原阶段: " + ", ".join(
            f"{e.get('stage')} {e['duration_ms']:.3f}ms" for e in stages))
        output.append(f"还原阶段耗时合计: {sum(e['duration_ms'] for e in stages):.3f} ms")
    return "\n".join(output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path, help="--trace 输出的 JSONL 文件")
    args = parser.parse_args()
    if not args.trace.is_file():
        parser.error(f"日志文件不存在：{args.trace}")
    try:
        print(analyze(args.trace))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(f"无法分析日志：{exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
