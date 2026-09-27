"""ACCESS_LOST 探针：只监听 DXGI Desktop Duplication 的失效事件，不发文件。

背景（issue #31 / report-output-sizes-live.md）：实投基准期间 dxcam 内部
「output-change recovery」（ACCESS_LOST 0x887A0026 等）高频出现，5 MiB 档
~83s 内 17 次，且每次 dxcam 采集线程 COMError 崩溃后重建期间丢帧、只能靠
整轮循环重播补齐。本探针回答「触发源是什么、多久一次」：

  1. 按实投同款路径建 dxcam（threaded capture，target_fps=120, video_mode）；
  2. 挂 logging handler 捕获 dxcam 内部恢复日志（即 ACCESS_LOST 的第一手
     时间戳，dxcam 走标准 logging，无需解析 stderr）；
  3. grab 循环镜像 _dxgi_capture：连续 grab 异常 / is_capturing 停止 /
     帧时间戳停滞 → 记录后按生产口径释放重建，继续观察；
  4. 事件逐条写 jsonl + 控制台实时一行；结束输出间隔统计，并与 Windows
     事件日志（System，Display / 显卡驱动 provider）做 ±2s 关联。

判读基线：空闲桌面几分钟一次属正常（他人应用触碰输出）；周期性触发
（固定间隔）指向定时任务 / DRR / 夜间模式；播放基准期间骤增则与浏览器
渲染叠加有关。

用法：
    .venv/Scripts/python.exe benchmark/probe_access_lost.py                  # 5 分钟
    .venv/Scripts/python.exe benchmark/probe_access_lost.py --duration 60    # 1 分钟
    .venv/Scripts/python.exe benchmark/probe_access_lost.py --duration 0     # 直到 Ctrl+C
"""

import argparse
import json
import logging
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from receiver.sources.desktop import _release_dxgi_camera  # noqa: E402

WORK_DIR = Path(__file__).resolve().parent / "results" / "work"
EVENT_LOG = WORK_DIR / "access_lost_probe.jsonl"

HRESULT_RE = re.compile(r"0x[0-9A-Fa-f]{8}")
GRAB_FAIL_REBUILD = 5        # 与 _dxgi_capture 同口径：连续失败才重建
STALL_TIMEOUT_S = 5.0        # 与 DXGI_FRAME_STALL_TIMEOUT_S 同口径


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


class DxcamEventCatcher(logging.Handler):
    """捕获 dxcam 内部日志（ACCESS_LOST 恢复、线程崩溃等），逐条回调。"""

    def __init__(self, on_record):
        super().__init__(level=logging.DEBUG)
        self.on_record = on_record

    def emit(self, record):
        msg = record.getMessage()
        m = HRESULT_RE.search(msg)
        self.on_record({
            "type": "dxcam_log",
            "level": record.levelname,
            "logger": record.name,
            "hresult": m.group(0) if m else None,
            "message": msg,
        })


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def correlate_event_log(events, window_s: float = 2.0) -> None:
    """System 事件日志与 ACCESS_LOST 时间做 ±window_s 关联（诊断触发源）。

    只筛 System 日志里 Error/Warning 且 provider 或消息含显卡/显示关键词
    的记录；无匹配不代表无因（多数模式切换不写事件日志），有匹配基本实锤。
    """
    kws = ("display", "nvlddmkm", "amdkmdag", "igfx", "nvhm", "dxgkrnl",
           "显卡", "显示")
    starts = min(e["t_iso"] for e in events) if events else None
    ends = max(e["t_iso"] for e in events) if events else None
    ps = f"""
$ev = Get-WinEvent -FilterHashtable @{{LogName='System';
  Level=1,2,3; StartTime=(Get-Date).AddMinutes(-120)}} -ErrorAction SilentlyContinue |
  Where-Object {{ ($_.ProviderName -match '{'|'.join(kws)}') -or
                  ($_.Message -match '{'|'.join(kws)}') }} |
  Select-Object TimeCreated, ProviderName, Id, LevelDisplayName, @{{
      n='Msg'; e={{ ($_.Message -split "`n")[0] }} }} | ConvertTo-Json -Compress
$ev
"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=60,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"[事件日志关联] 调用失败：{e}", flush=True)
        return
    if not out:
        print("[事件日志关联] 近 120 分钟无匹配的 Error/Warning 显示类事件", flush=True)
        return
    recs = json.loads(out if out.startswith("[") else f"[{out}]")
    hits = []
    for ev in events:
        t = datetime.fromisoformat(ev["t_iso"])
        for r in recs:
            rt = datetime.fromisoformat(r["TimeCreated"])
            if abs((rt - t).total_seconds()) <= window_s:
                hits.append((ev, r))
    if not hits:
        print(f"[事件日志关联] {len(recs)} 条显示类事件均未与 "
              f"{len(events)} 次 ACCESS_LOST ±{window_s:.0f}s 对上"
              "（触发源不写事件日志，属正常——继续按周期性排查）", flush=True)
        return
    print(f"[事件日志关联] {len(hits)} 对时间吻合：", flush=True)
    for ev, r in hits[:20]:
        print(f"  {ev['t_iso']} {ev['type']} hresult={ev.get('hresult')} ↔ "
              f"{r['TimeCreated']} [{r['ProviderName']}] id={r['Id']} {r['Msg'][:80]}",
              flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--duration", type=float, default=300,
                    help="监听秒数；0 = 直到 Ctrl+C（默认 300）")
    args = ap.parse_args()

    import dxcam  # 与实投同款路径

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    events: list[dict] = []
    lock = threading.Lock()

    def on_event(ev: dict) -> None:
        rec = {"t_iso": now_iso(), "t_mono": round(time.monotonic(), 3), **ev}
        with lock:
            events.append(rec)
        with (EVENT_LOG).open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"[{rec['t_iso']}] {ev['type']} "
              f"hresult={ev.get('hresult') or '-'} {ev.get('message', '')[:110]}",
              flush=True)

    handler = DxcamEventCatcher(on_event)
    dxcam_logger = logging.getLogger("dxcam")
    dxcam_logger.addHandler(handler)
    dxcam_logger.setLevel(logging.DEBUG)

    camera = None
    grab_failures = 0
    rebuilds = 0
    stall_since = None
    last_ticks = None
    print(f"探针启动：监听 {args.duration or '∞'}s，事件落盘 {EVENT_LOG}", flush=True)
    print("（无需遮挡屏幕；期间正常使用电脑即为真实触发环境）", flush=True)
    t0 = time.monotonic()
    try:
        camera = dxcam.create(output_color="GRAY")
        camera.start(target_fps=120, video_mode=True)
        while True:
            if args.duration and time.monotonic() - t0 >= args.duration:
                break
            try:
                frame = camera.grab(new_frame_only=True)
                grab_failures = 0
            except Exception as e:  # noqa: BLE001  duplication 失效即观察对象
                grab_failures += 1
                hr = getattr(e, "args", [None])[0]
                on_event({"type": "grab_error",
                          "hresult": f"0x{hr & 0xFFFFFFFF:08X}"
                                     if isinstance(hr, int) else None,
                          "message": f"{type(e).__name__}: {e}"})
                if grab_failures >= GRAB_FAIL_REBUILD:
                    on_event({"type": "rebuild", "message":
                              f"连续 grab 失败 {grab_failures} 次，释放重建（生产口径）"})
                    _release_dxgi_camera(camera)
                    camera = None
                    grab_failures = 0
                    rebuilds += 1
                time.sleep(0.2)
                continue
            if not getattr(camera, "is_capturing", True):
                on_event({"type": "thread_dead",
                          "message": "threaded capture 线程已停止（内部 COMError）"})
                _release_dxgi_camera(camera)
                camera = dxcam.create(output_color="GRAY")
                camera.start(target_fps=120, video_mode=True)
                rebuilds += 1
                stall_since = None
                continue
            ticks = getattr(camera, "latest_frame_ticks", None)
            now = time.monotonic()
            if ticks is None or ticks == last_ticks:
                if stall_since is None:
                    stall_since = now
                elif now - stall_since >= STALL_TIMEOUT_S:
                    on_event({"type": "stall", "message":
                              f"帧时间戳 {STALL_TIMEOUT_S:.0f}s 未推进，视为 duplication 失效"})
                    _release_dxgi_camera(camera)
                    camera = dxcam.create(output_color="GRAY")
                    camera.start(target_fps=120, video_mode=True)
                    rebuilds += 1
                    stall_since = None
            else:
                last_ticks = ticks
                stall_since = now
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\n[中断]", flush=True)
    finally:
        if camera is not None:
            _release_dxgi_camera(camera)

    # ---- 汇总 ----
    recoveries = [e for e in events if e["type"] == "dxcam_log"
                  and "access loss" in e.get("message", "").lower()]
    total_s = time.monotonic() - t0
    print(f"\n===== 汇总（监听 {total_s:.0f}s，事件 {len(events)} 条）=====", flush=True)
    print(f"  dxcam ACCESS_LOST/output-change 恢复：{len(recoveries)} 次"
          f"（{len(recoveries) / (total_s / 60):.1f} 次/分钟）", flush=True)
    by_type: dict[str, int] = {}
    for e in events:
        by_type[e["type"]] = by_type.get(e["type"], 0) + 1
    print(f"  事件分类：{by_type}", flush=True)
    print(f"  探针侧相机重建：{rebuilds} 次", flush=True)
    if len(recoveries) >= 2:
        gaps = [b["t_mono"] - a["t_mono"]
                for a, b in zip(recoveries, recoveries[1:])]
        gaps_sorted = sorted(gaps)
        median = gaps_sorted[len(gaps_sorted) // 2]
        print(f"  恢复间隔：min {min(gaps):.1f}s / 中位 {median:.1f}s / "
              f"max {max(gaps):.1f}s", flush=True)
        if max(gaps_sorted) - min(gaps_sorted) < 2:
            print("  → 间隔高度一致：周期性触发源（定时任务 / DRR / 夜间模式）嫌疑最大",
                  flush=True)
        elif sum(1 for g in gaps if g < 1) > len(gaps) * 0.5:
            print("  → 多为秒内连发：单次过渡引发连环失效（重建风暴），"
                  "重点看恢复策略而非触发源", flush=True)
    hresult_count: dict[str, int] = {}
    for e in events:
        hr = e.get("hresult")
        if hr:
            hresult_count[hr] = hresult_count.get(hr, 0) + 1
    if hresult_count:
        print(f"  HRESULT 分布：{hresult_count}", flush=True)

    if events:
        try:
            do_corr = input("是否关联 Windows 事件日志（System, ±2s）？[Y/n] ")
        except (EOFError, OSError):  # 非交互运行：默认执行关联
            do_corr = ""
        if do_corr.strip().lower() not in ("n", "no"):
            correlate_event_log(events)
    return 0


if __name__ == "__main__":
    sys.exit(main())
