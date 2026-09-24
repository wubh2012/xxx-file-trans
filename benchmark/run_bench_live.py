"""实投 desktop 闭环基准（benchmark-v2，B1 验证，issue #28/#29 实战首秀）。

与 run_bench.py（v1 离线）的分工：v1 测「帧数规模 × 解码速率」，FPS 只是
信道模型折算参数；本脚本补上 v1 明示的已知边界——**真实播放节奏下
desktop 抓屏的丢帧与吞吐**，直接检验 speedup-methods.md B1 的前提
「60Hz 屏 + desktop 抓屏理论可到 50–60」。

流程（全自动，需真实屏幕，运行期间勿遮挡浏览器窗口）：
  1. playwright 有头启动 chromium，窗口模式加载 sender.html（窗口置顶置前，
     位置 0,0，尽量大以降低帧数规模）；
  2. 每个 FPS 档：设 FPS → 载入随机文件自动播放 → 由页面 JS 实测画布
     getBoundingClientRect + devicePixelRatio 换算物理抓屏区域（外扩余量，
     裁切容忍 #22 兜底）→ 采一帧 decode 自检对准 → calibrate 抓 1 轮循环
     （缺帧率 / 重复帧 / 实测到达 FPS，issue #29 口径）→ 3 次接收计时；
  3. 产出 live_desktop_results.json / .csv，人读分析另见 report-live.md。

口径：
  - wall_s = 播放开始（文件载入）→ 还原写盘完成，含最多一整轮循环等待，
    即实投吞吐口径；elapsed_from_start_s = 接收线程启动 → 写盘完成；
  - 缺帧/到达 FPS 判级来自 calibrate（deadline 干净收尾，非 Ctrl+C 部分
    报告，判级有效）；
  - 每轮接收重新生成随机文件：新 file_id → 新进度目录，断点续传不串轮；
    进度目录经 receiver.paths.PROGRESS_DIR 重定向到 benchmark/results/work/，
    不污染接收端真实目录；
  - mss 抓屏与解码同进程（OpenCV 释 GIL），浏览器独立进程，负载与真实
    使用一致；单变量只扫 FPS，窗口/BIT/文件大小固定。

用法：
    .venv/Scripts/python.exe benchmark/run_bench_live.py           # 全量约 10 分钟
    .venv/Scripts/python.exe benchmark/run_bench_live.py --quick   # 冒烟 1–2 分钟
"""

import argparse
import csv
import gzip
import hashlib
import json
import os
import random
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from receiver import paths  # noqa: E402
from receiver.calibrate import run_calibration  # noqa: E402
from receiver.pipeline import FrameRejected, decode_frame  # noqa: E402
from receiver.run import run_receive  # noqa: E402
from receiver.sources.desktop import iter_desktop  # noqa: E402  # 导入即声明 DPI 感知

# ---------- 基准参数（单变量扫 FPS，其余固定） ----------
# 冒烟实测（2026-09-24）：FPS 30/60 时实收节拍仅 5.75/0.2 fps（稳定闸门两帧
# 一致判定在抓屏周期 > 显示周期时永不满足，只剩 5s 超时强放），30/45/60 全灭。
# 全量改为向下扫 {10,15,20,30} 精确定位当前实现（mss + 全图解码串行）的实际
# 安全上限；B1 的「抓屏可跑 50–60」留待 C1/C2/B2 落地后复测。

FPS_LIST = [10, 15, 20, 30]
SIZE_MIB = 5
BIT_CSS = 8                      # CSS px/块；物理 BIT = ×dpr，>15 时报错退出
PAD = 4                          # sender.html 默认
REPS = 3                         # 每档接收计时次数
CALIB_CYCLES = 1                 # calibrate 抓几轮循环（1 轮足够缺帧判级）
GRACE_S = 10.0                   # calibrate / 自检 / 超时的宽限秒数
REGION_PAD_DIP = 80              # 抓屏区域四周外扩（DIP）：裁切容忍兜底 + 勿贴边
WINDOW_POS = (0, 0)
WINDOW_SIZE = (1920, 1040)       # 请求值，实际被屏幕/任务栏钳制时以 JS 实测为准

RESULTS_DIR = Path(__file__).resolve().parent / "results"
WORK_DIR = RESULTS_DIR / "work"

# Windows 控制台 GBK 下中文判定行会花屏，统一 UTF-8（读输出方按 UTF-8 解）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


class LiveReporter:
    """run_receive 的宽容 reporter：实投过渡帧被拒是正常现象，计数不上抛。"""

    def __init__(self):
        self.rejected = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def set_total(self, total, completed=None):
        pass

    def on_decoded(self, payload, is_new):
        pass

    def on_rejected(self, name, e):
        self.rejected += 1

    def finish(self):
        pass


def compute_region(page) -> tuple[dict, float]:
    """页面 JS 实测画布位置 → mss 物理像素区域（外扩 REGION_PAD_DIP，钳到虚拟屏）。

    坐标链：canvas rect（视口 DIP）+ 视口原点（window.screenX/Y + 窗口
    chrome 偏移）= 屏幕 DIP → × devicePixelRatio = 物理 px（mss 经
    per-monitor DPI awareness 与物理坐标逐像素对齐）。chrome 偏移按
    边框对称近似，误差被外扩余量 + 裁切容忍（#22）吸收。"""
    rect, meta = page.evaluate("""() => {
      const r = document.getElementById('stage').getBoundingClientRect();
      return [{x: r.x, y: r.y, w: r.width, h: r.height},
              {dpr: devicePixelRatio, sx: window.screenX, sy: window.screenY,
               ow: window.outerWidth, oh: window.outerHeight,
               iw: window.innerWidth, ih: window.innerHeight}];
    }""")
    dpr = meta["dpr"]
    vx = meta["sx"] + (meta["ow"] - meta["iw"]) / 2   # 左右边框对称近似
    vy = meta["sy"] + (meta["oh"] - meta["ih"])       # 顶部 chrome 全算在 y
    left = round((vx + rect["x"] - REGION_PAD_DIP) * dpr)
    top = round((vy + rect["y"] - REGION_PAD_DIP) * dpr)
    width = round((rect["w"] + 2 * REGION_PAD_DIP) * dpr)
    height = round((rect["h"] + 2 * REGION_PAD_DIP) * dpr)
    import mss
    with mss.mss() as sct:
        mon = sct.monitors[0]   # 全部显示器虚拟屏并集（物理坐标）
        left = max(mon["left"], left)
        top = max(mon["top"], top)
        width = min(mon["left"] + mon["width"] - left, width)
        height = min(mon["top"] + mon["height"] - top, height)
    return {"left": left, "top": top, "width": width, "height": height}, dpr


def grab_region_gray(region: dict) -> np.ndarray:
    """抓一帧区域灰度图（自检用；采集主循环走 iter_desktop）。"""
    import mss
    with mss.mss() as sct:
        shot = sct.grab(region)
        return cv2.cvtColor(np.asarray(shot), cv2.COLOR_BGRA2GRAY)


def alignment_selfcheck(region: dict, tries: int = 20) -> bool:
    """区域对准自检：连抓若干帧，任一帧完整解码（数据/元数据帧皆可）即通过。"""
    for _ in range(tries):
        try:
            decode_frame(grab_region_gray(region))
            return True
        except FrameRejected:
            time.sleep(0.25)
    return False


def frames_with_deadline(gen, deadline: float):
    """到 deadline 干净收尾（生成器 return → run_calibration 不打 interrupted
    标记，缺帧判级有效；与 Ctrl+C 的部分报告路径区分）。"""
    for name, img in gen:
        yield name, img
        if time.perf_counter() >= deadline:
            return


def receive_worker(region: dict, out_dir: Path, raw: bytes, box: dict) -> None:
    """接收线程主体：结果与拒帧计数写入 box（线程内启动 mss，随循环播放
    收齐后自然结束；file_id 每轮唯一，滞留不串轮）。"""
    rep = LiveReporter()
    box["reporter"] = rep
    t0 = time.perf_counter()
    result = run_receive(iter_desktop(region=region), out_dir,
                         reporter_factory=lambda: rep, notify=None)
    box["result"] = result
    box["worker_s"] = time.perf_counter() - t0


def verdict_line(report: dict) -> str:
    """calibrate 报告 → 一行人读判级（与 format_report 三级口径一致）。"""
    transfers = report.get("transfers") or []
    if not transfers:
        return "无数据帧可判级"
    t = transfers[0]
    if report.get("interrupted"):
        return f"统计中断：缺 {t['missing']}/{t['totalFrames']}（不作判级依据）"
    if t["totalFrames"] and t["missing"] == 0:
        return f"缺帧 0/{t['totalFrames']}：实测干净，可尝试再上一档"
    if t["totalFrames"] and t["missingRate"] <= 0.01:
        return (f"缺帧 {t['missing']}/{t['totalFrames']}"
                f"（{t['missingRate']:.1%}）：边缘，建议降一档复测")
    return (f"缺帧 {t['missing']}/{t['totalFrames']}"
            f"（{t['missingRate']:.1%}）：显著，建议降档")


def load_and_play(page, path: Path, fps: int) -> None:
    """载入文件并确保进入播放态（自愈）。

    同路径重复 set_input_files 的 change 事件在 Chromium 下不可靠（e2e
    从未覆盖双次载入），故：A/B 路径交替 + 载前清空 input value；仍失败
    则读 status 诊断后整页 reload 重来（窗口尺寸不变，抓屏区域仍有效）。"""
    page.evaluate("() => { document.getElementById('file').value = ''; }")
    page.set_input_files("#file", str(path))
    try:
        page.wait_for_selector("body.playing", timeout=10000)
        return
    except Exception:
        status = page.text_content("#status") or ""
        print(f"  [warn] 载入未进入播放态（status={status!r}），整页重载重试",
              flush=True)
    page.reload()
    page.wait_for_selector("#modeWindow", timeout=10000)
    page.click("#modeWindow")
    page.fill("#fps", str(fps))
    page.evaluate("() => { document.getElementById('file').value = ''; }")
    page.set_input_files("#file", str(path))
    page.wait_for_selector("body.playing", timeout=15000)


def main() -> int:
    global FPS_LIST, SIZE_MIB, REPS, CALIB_CYCLES
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--quick", action="store_true",
                    help="冒烟：1MiB × FPS {30,60} × 1 次接收 × 1 轮校准")
    args = ap.parse_args()
    if args.quick:
        FPS_LIST, SIZE_MIB, REPS, CALIB_CYCLES = [30, 60], 1, 1, 1

    from playwright.sync_api import sync_playwright

    # 进度目录重定向（run_receive 内 paths.PROGRESS_DIR 运行时查属性，可猴补）
    paths.PROGRESS_DIR = WORK_DIR / "progress"
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    sender_uri = (ROOT / "sender.html").as_uri()
    runs, calibs = [], []
    payload_a = WORK_DIR / "live_payload_a.bin"
    payload_b = WORK_DIR / "live_payload_b.bin"
    payload_toggle = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=[f"--window-position={WINDOW_POS[0]},{WINDOW_POS[1]}",
                  f"--window-size={WINDOW_SIZE[0]},{WINDOW_SIZE[1]}"],
        )
        ctx = browser.new_context(no_viewport=True)
        page = ctx.new_page()
        page.goto(sender_uri)
        page.click("#modeWindow")
        page.bring_to_front()

        for fps in FPS_LIST:
            print(f"=== FPS {fps} ===", flush=True)
            raw = os.urandom(SIZE_MIB * 1024 * 1024)
            payload_toggle += 1
            raw_path = payload_a if payload_toggle % 2 else payload_b
            raw_path.write_bytes(raw)
            page.fill("#fps", str(fps))

            # 载入文件自动播放（窗口模式），页面 JS 实测几何与画布
            load_and_play(page, raw_path, fps)
            page.bring_to_front()
            geo = page.evaluate("() => ({fps: __sender.state.geo.FPS,"
                                " bit: __sender.state.geo.BIT,"
                                " cols: __sender.state.geo.COLS,"
                                " rows: __sender.state.geo.ROWS,"
                                " chunk: __sender.state.geo.chunkSize,"
                                " total: __sender.state.totalFrames})")
            assert geo["fps"] == fps, f"发送端 FPS {geo['fps']} != 设定 {fps}"
            if geo["bit"] > 15:
                raise RuntimeError(f"物理 BIT {geo['bit']} 超出帧头 4 bit 上限，"
                                   "请降低显示缩放或 BIT")
            region, dpr = compute_region(page)
            cycle_s = (geo["total"] + geo["total"] // 100 + 1) / fps
            print(f"  物理几何 BIT={geo['bit']} {geo['cols']}x{geo['rows']}，"
                  f"数据帧 {geo['total']}，理论循环 {cycle_s:.1f}s，dpr={dpr}",
                  flush=True)

            if not alignment_selfcheck(region):
                raise RuntimeError("抓屏区域自检失败：20 次捕获无一完整解码，"
                                   "检查窗口遮挡 / 坐标换算")
            print("  抓屏对准自检通过", flush=True)

            # calibrate：抓 CALIB_CYCLES 轮 + 宽限，deadline 干净收尾判级
            calib_s = CALIB_CYCLES * cycle_s + GRACE_S
            report = run_calibration(
                frames_with_deadline(iter_desktop(region=region),
                                     time.perf_counter() + calib_s))
            fps_stat = report.get("measuredFps")
            calibs.append({"fps": fps, "calib_s": round(calib_s, 1),
                           "measuredFps": fps_stat, "report": report,
                           "verdict": verdict_line(report)})
            m = (f"，实测到达 FPS 中位 {fps_stat['median']}"
                 f"（p5 {fps_stat['p5']} / p95 {fps_stat['p95']}）") if fps_stat else ""
            print(f"  calibrate：{verdict_line(report)}{m}", flush=True)

            # REPS 次接收计时：每轮新随机文件（新 file_id 新进度目录），
            # 接收线程先就位 → 载文件触发播放并起表
            tier = {"fps": fps, "geo": geo, "cycle_s": round(cycle_s, 1),
                    "dpr": dpr, "region": region, "reps": []}
            for rep in range(REPS):
                raw = os.urandom(SIZE_MIB * 1024 * 1024)
                payload_toggle += 1
                raw_path = payload_a if payload_toggle % 2 else payload_b
                raw_path.write_bytes(raw)
                page.keyboard.press("Escape")     # 停上一轮播放，UI 回来
                page.wait_for_selector("body.playing", state="detached",
                                       timeout=5000)
                out_dir = WORK_DIR / f"live_out_{fps}_{rep}"
                expect_total = -(-len(gzip.compress(raw)) // geo["chunk"])
                # 实收节拍 ≪ 播放 FPS 时一轮不止 cycle_s，按 6 轮给足
                timeout_s = 6 * (expect_total / fps) + GRACE_S + 15

                box: dict = {}
                th = threading.Thread(target=receive_worker,
                                      args=(region, out_dir, raw, box),
                                      daemon=True)
                th.start()
                time.sleep(0.3)                   # 接收端就位（mss 打开）
                t0 = time.perf_counter()
                load_and_play(page, raw_path, fps)
                page.bring_to_front()
                th.join(timeout=timeout_s)
                wall = time.perf_counter() - t0

                if "result" in box:
                    result = box["result"]
                    r = {"rep": rep, "fps": fps, "size_mib": SIZE_MIB,
                         "failed": result.code != 0,
                         "wall_s": round(wall, 3),
                         "elapsed_s": round(result.elapsed, 3),
                         "received": result.received, "total": result.total,
                         "rejected": box["reporter"].rejected,
                         "sha_ok": bool(result.sha256) and
                                   result.sha256 == hashlib.sha256(raw).hexdigest(),
                         "error": result.error}
                else:
                    r = {"rep": rep, "fps": fps, "size_mib": SIZE_MIB,
                         "failed": True, "wall_s": round(wall, 3),
                         "note": f"超时 {timeout_s:.0f}s 未还原（滞留线程随循环自行收尾）"}
                tier["reps"].append(r)
                ok = "OK" if not r.get("failed") else "失败"
                print(f"  rep{rep}: {ok} wall={r.get('wall_s')}s "
                      f"elapsed={r.get('elapsed_s', '-')}s "
                      f"收 {r.get('received', '-')}/{r.get('total', '-')} "
                      f"拒 {r.get('rejected', '-')} "
                      f"sha={'✓' if r.get('sha_ok') else '✗'}", flush=True)
            runs.append(tier)

            page.keyboard.press("Escape")
            page.wait_for_selector("body.playing", state="detached", timeout=5000)

        browser.close()

    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "live-desktop",
        "fps_list": FPS_LIST, "size_mib": SIZE_MIB, "reps": REPS,
        "calib_cycles": CALIB_CYCLES, "bit_css": BIT_CSS, "pad": PAD,
        "window": list(WINDOW_SIZE), "region_pad_dip": REGION_PAD_DIP,
        "python": sys.version.split()[0],
        "note": "wall_s = 文件载入(播放开始)→还原写盘完成（实投吞吐口径，含最多"
                "一整轮等待）；elapsed_s = run_receive elapsed（首数据帧落地起）；"
                "缺帧/实测到达 FPS 为 calibrate 口径（issue #29）",
    }
    (RESULTS_DIR / "live_desktop_results.json").write_text(
        json.dumps({"meta": meta, "calibrations": calibs, "runs": runs},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    with (RESULTS_DIR / "live_desktop_results.csv").open(
            "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["fps", "rep", "size_mib", "cycle_s", "wall_s", "elapsed_s",
                    "received", "total", "rejected", "sha_ok", "failed"])
        for tier in runs:
            for r in tier["reps"]:
                w.writerow([tier["fps"], r.get("rep"), r.get("size_mib"),
                            tier["cycle_s"], r.get("wall_s"), r.get("elapsed_s"),
                            r.get("received"), r.get("total"), r.get("rejected"),
                            r.get("sha_ok"), r.get("failed")])
    print(f"\n结果已写入 {RESULTS_DIR / 'live_desktop_results.json'} 与 .csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
