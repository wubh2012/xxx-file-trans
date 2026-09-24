"""CLI 入口：python -m receiver（docs 需求文档 §6 契约）。

退出码：0 = 还原成功 / 统计完成；1 = 未收齐 / 还原失败 / 无帧可统计；2 = 用法错误。
"""

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console

from receiver import paths
from receiver.calibrate import format_report, run_calibration
from receiver.notify import default_notifier
from receiver.pick import pick_region
from receiver.protocol import startup_self_check
from receiver.run import run_receive
from receiver.sources import iter_source
from receiver.sources.desktop import format_region, iter_desktop, parse_region
from receiver.sources.video import iter_video
from receiver.summary import restore_summary

# 摘要行 / 未完成统计的打印出口（issue #25）：rich 高亮，非 tty 自动
# 降级为普通文本；soft_wrap 防长文件名折行拆散断言口径
_console = Console()
_err_console = Console(stderr=True)


class PickCancelled(Exception):
    """--region pick 用户取消（Esc / 关闭覆盖窗）：明确中止，不回退整屏（issue #22）。"""


def receive_frames(args, frames, notify=None) -> int:
    """接收主循环 CLI 壳（issue #26 重构）：核心逻辑在 receiver.run.run_receive
    （与 tkinter GUI 共用），本壳只按 ReceiveResult 字段渲染 CLI 文本，
    输出契约（issue #25 摘要口径）不变。

    notify 为可注入的完成通知边界（issue #12）：还原成功后以
    notify(title, message) 报喜，失败路径不调用；None 时不通知
    （真实默认实现见 receiver.notify）。"""
    result = run_receive(frames, Path(args.out), notify=notify)
    if result.error:
        print(result.error, file=sys.stderr)
    if result.incomplete:
        _err_console.print(
            result.incomplete,
            style="yellow dim", markup=False, highlight=False, soft_wrap=True,
        )
    if result.code == 0 and result.dest is not None:
        # 还原统计摘要（issue #25）：单行 rich 高亮，友好大小 / 耗时 /
        # 明文口径速率 / 帧 N/M / sha256；与进度条压缩口径 KB/s 并存不混用
        _console.print(
            restore_summary(result.name, result.plain_size, result.elapsed,
                            result.received, result.total, result.sha256),
            style="bold green", markup=False, highlight=False, soft_wrap=True,
        )
    for warning in result.warnings:
        print(warning, file=sys.stderr)
    return result.code


def calibrate_frames(args, frames) -> int:
    """calibrate 统计模式（issue #11）：只统计不还原——不写还原文件、
    不建任务目录，缺帧不影响统计完成。输出人读摘要 + 末行 JSON
    （推荐参数 BIT/PAD，回灌发送端）。Ctrl+C（desktop 等无限源）时
    输出已统计部分——采样边界由操作者掌握。

    实测到达 FPS 仅对实时源有意义（issue #29）：images 源的磁盘读图
    速度不是传输节拍，JSON 置 None、人读报告不显示，避免假信号。"""
    report = run_calibration(frames)
    if args.source == "images":
        report["measuredFps"] = None
    if report["frames"] == 0:
        print("无帧可统计：取帧源未产出任何画面", file=sys.stderr)
        return 1
    if report["interrupted"]:
        print("统计中断（Ctrl+C）：输出已统计部分", file=sys.stderr)
    print(format_report(report))
    print(json.dumps(report, ensure_ascii=False))
    return 0


def _add_source_args(p) -> None:
    """receive / calibrate 共用的取帧源参数面（choices 与 receive 一致）。"""
    p.add_argument("--source", required=True, choices=["images", "desktop", "video", "camera"], help="取帧源")
    p.add_argument("--dir", type=Path, help="images 源：PNG 帧序列目录")
    p.add_argument("--video", type=Path, help="video 源：录制视频文件")
    p.add_argument("--region", help="desktop 源：捕获区域 L,T,W,H，或 pick 冻屏框选")


def _desktop_region(parser, args) -> dict | None:
    """desktop 源捕获区域（需求 F10 + issue #22）：`L,T,W,H` 解析为 mss
    区域 dict；`pick` 为交互框选特例值（不新增子命令，receive / calibrate
    共用）——确认后回显等效参数，取消抛 PickCancelled（不静默回退整屏）。"""
    if args.region == "pick":
        region = pick_region()
        if region is None:
            raise PickCancelled()
        print(f"已框选区域：--region {format_region(region)}"
              f"（下次可直接粘贴跳过框选）")
        return region
    try:
        return parse_region(args.region) if args.region else None
    except ValueError as e:
        parser.error(str(e))
        return 2  # 不可达（parser.error 直接退出），仅供类型检查


def _frames_or_error(parser, args):
    """取帧源分派（receive / calibrate 共用）：返回 (名称, 灰度图) 迭代器。
    参数缺失时按用法错误退出（退出码 2）；camera 骨架在 main 收口。
    video 生成器惰性打开，打不开的 ValueError 在首次迭代抛出，
    由调用方包住消费端转为退出码 2。"""
    if args.source == "images":
        if not args.dir:
            parser.error("--source images 需要 --dir <PNG 帧序列目录>")
        return iter_source("images", args.dir)
    if args.source == "desktop":
        return iter_desktop(region=_desktop_region(parser, args))
    # video（#10）：缺参用法错误；打不开的报错见调用方
    if not args.video:
        parser.error("--source video 需要 --video <录制视频文件>")
    return iter_video(args.video)


def _anchor_dirs(args) -> None:
    """任务目录锚定（需求 N4）：缺省输出锚定 output/，progress / debug 随建。"""
    if args.out is None:
        args.out = paths.OUTPUT_DIR
    paths.PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
    paths.DEBUG_DIR.mkdir(parents=True, exist_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="receiver", description="跨隔离网络单向文件摆渡接收端")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("receive", help="从取帧源解码还原文件")
    _add_source_args(p)
    p.add_argument("--out", type=Path, default=None,
                   help="还原输出目录（默认锚定脚本目录 output/，不依赖 cwd；显式给定值语义不变）")
    c = sub.add_parser("calibrate", help="统计识别率 / CRC 通过率，输出推荐参数（不还原不落盘）")
    _add_source_args(c)
    args = parser.parse_args(argv)

    try:
        startup_self_check()  # 协议 §3：两端启动时必须自检通过
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2

    if args.source == "camera":
        # camera 骨架（issue #13）：统一迭代器接口占位（receiver/sources/camera.py），
        # 完整摄像头采集不在本票范围；receive / calibrate 一致提示后退出
        print("camera 源为接口骨架（issue #13），完整摄像头采集尚未实现", file=sys.stderr)
        return 2

    if args.command == "calibrate":
        # 统计模式（issue #11）：不落盘不建目录，故无 _anchor_dirs；
        # Ctrl+C（desktop 等无限源）由 calibrate_frames 输出部分报告
        try:
            return calibrate_frames(args, _frames_or_error(parser, args))
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        except PickCancelled:
            print("已取消框选（Esc）：采集中止", file=sys.stderr)
            return 1

    # 完成通知（issue #12）：工厂自选 winotify 桌面通知或终端高亮降级，
    # 一处接线，仅 receive 路径使用
    notify = default_notifier()
    try:
        frames = _frames_or_error(parser, args)
    except PickCancelled:
        print("已取消框选（Esc）：采集中止", file=sys.stderr)
        return 1
    # 目录锚定（需求 N4）：默认输出与 progress / debug 目录锚定脚本目录，
    # 不依赖 cwd；显式 --out 语义不变
    _anchor_dirs(args)
    try:
        return receive_frames(args, frames, notify=notify)
    except ValueError as e:
        # video 源打不开（不存在 / 编码器不支持）：显式报错，不静默零帧；
        # 生成器惰性打开，首次迭代才抛，故包住消费端
        print(str(e), file=sys.stderr)
        return 2
