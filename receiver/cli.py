"""CLI 入口：python -m receiver（docs 需求文档 §6 契约）。

退出码：0 = 还原成功；1 = 未收齐 / 还原失败；2 = 用法错误。
"""

import argparse
import hashlib
import sys
from pathlib import Path

from receiver import paths
from receiver.pipeline import DecodedFrame, FrameRejected, decode_frame
from receiver.progress import ProgressReporter
from receiver.protocol import startup_self_check
from receiver.restore import RestoreError, gunzip_verify
from receiver.sanitize import safe_dest, sanitize_filename
from receiver.sources import iter_source
from receiver.sources.desktop import iter_desktop, parse_region
from receiver.store import FrameStore, IncompleteError


def receive_frames(args, frames) -> int:
    """接收主循环：frames 为任一取帧源的 (名称, 灰度图) 迭代器（spec「模块划分」）。"""
    # 断点续传（issue #7）：任务目录锚定 progress/，首个数据帧落地即锁定，
    # 崩溃 / Ctrl+C 重启后惰性加载已收帧，只补缺失帧
    store = FrameStore(paths.PROGRESS_DIR)
    try:
        # 实时进度（需求 F15）：帧数 / 百分比 / KB/s / 识别率 / 丢帧，
        # CRC 连续失败告警限流；逐帧丢弃打印与收帧汇总均由 reporter 接管
        with ProgressReporter() as reporter:
            try:
                for name, img in frames:
                    try:
                        frame: DecodedFrame = decode_frame(img)
                    except FrameRejected as e:
                        reporter.on_rejected(name, e)
                        continue
                    try:
                        is_new = store.add(frame)
                    except FrameRejected as e:
                        # 跨任务混帧 / 参数锁定硬锁（param_lock）同样整帧拒绝
                        reporter.on_rejected(name, e)
                        continue
                    reporter.on_decoded(frame.payload, is_new)
                    if store.total_frames is not None:
                        reporter.set_total(store.total_frames,
                                           completed=store.received_count())
            except KeyboardInterrupt:
                print("接收中断（Ctrl+C）：进度已持久化，重新运行将只补缺失帧",
                      file=sys.stderr)
                return 1
            reporter.finish()
    finally:
        store.close()

    if store.is_complete():
        try:
            plain = gunzip_verify(store.assemble())
        except (IncompleteError, RestoreError) as e:
            print(f"还原失败：{e}", file=sys.stderr)
            return 1
        if len(plain) != store.metadata.plain_size:
            print(
                f"还原失败：plainSize 不一致（元数据声明 {store.metadata.plain_size}，"
                f"实际解压 {len(plain)}）",
                file=sys.stderr,
            )
            return 1
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            dest = safe_dest(out_dir, sanitize_filename(store.metadata.name))
        except ValueError as e:
            print(f"还原失败：{e}", file=sys.stderr)
            return 1
        dest.write_bytes(plain)
        print(f"还原完成：{dest}（{len(plain)} 字节，sha256 {hashlib.sha256(plain).hexdigest()[:16]}…）")
        return 0

    if store.data_complete():
        # §4：元数据帧是落盘文件名与还原截断长度的唯一来源，缺失不启动还原
        print("元数据缺失：数据帧照常收下，不启动还原", file=sys.stderr)
        return 1

    print("未收齐全部数据帧，不启动还原", file=sys.stderr)
    return 1


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
    p.add_argument("--source", required=True, choices=["images", "desktop", "video", "camera"], help="取帧源")
    p.add_argument("--dir", type=Path, help="images 源：PNG 帧序列目录")
    p.add_argument("--video", type=Path, help="video 源：录制视频文件")
    p.add_argument("--region", help="desktop 源：捕获区域 L,T,W,H")
    p.add_argument("--out", type=Path, default=None,
                   help="还原输出目录（默认锚定脚本目录 output/，不依赖 cwd；显式给定值语义不变）")
    args = parser.parse_args(argv)

    try:
        startup_self_check()  # 协议 §3：两端启动时必须自检通过
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2

    if args.source == "images":
        if not args.dir:
            parser.error("--source images 需要 --dir <PNG 帧序列目录>")
        # 目录锚定（需求 N4）：默认输出与 progress / debug 目录锚定脚本目录，
        # 不依赖 cwd；显式 --out 语义不变
        _anchor_dirs(args)
        return receive_frames(args, iter_source("images", args.dir))

    if args.source == "camera":
        # camera 骨架（issue #13）：统一迭代器接口占位（receiver/sources/camera.py），
        # 完整摄像头采集不在本票范围，明确提示后退出
        print("camera 源为接口骨架（issue #13），完整摄像头采集尚未实现", file=sys.stderr)
        return 2

    if args.source == "desktop":
        # 捕获区域（需求 F10）：`L,T,W,H` 解析为 mss 区域 dict，缺省全屏
        try:
            region = parse_region(args.region) if args.region else None
        except ValueError as e:
            parser.error(str(e))
            return 2  # 不可达（parser.error 直接退出），仅供类型检查
        _anchor_dirs(args)
        return receive_frames(args, iter_desktop(region=region))

    print(f"源 {args.source} 尚未实现（video: #10）", file=sys.stderr)
    return 2
