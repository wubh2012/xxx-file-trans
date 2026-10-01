import json

from benchmark.analyze_receive_trace import analyze


def test_interleaved_frame_number_backtracks_are_not_reported_as_replays(tmp_path):
    trace = tmp_path / "trace.jsonl"
    events = [{"event": "run_metadata", "total_frames": 2048,
               "playback_order": "interleaved"}]
    events += [{"event": "frame", "frame_no": no, "frame_kind": "data",
                "is_new": True, "t_rel_s": i / 30}
               for i, no in enumerate([0, 1024, 2016, 1, 1025, 2017, 2])]
    trace.write_text("\n".join(json.dumps(x) for x in events), encoding="utf-8")
    report = analyze(trace)
    assert "交错播放：帧号回退不用于统计轮次" in report
    assert "区间 1 新帧 7 帧" in report
