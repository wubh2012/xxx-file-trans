"""CLI 入口：python -m receiver（docs 需求文档 §6 契约）。

退出码：0 = 还原成功；1 = 未收齐 / 还原失败；2 = 用法错误。
"""

import argparse
import hashlib
import sys
from collections import Counter
from pathlib import Path

from receiver.pipeline import DecodedFrame, FrameRejected, decode_frame
from receiver.protocol import startup_self_check
from receiver.restore import RestoreError, gunzip_verify
from receiver.sources import iter_source
from receiver.store import FrameStore, IncompleteError


def receive_images(args) -> int:
    store = FrameStore()
    discard = Counter()

    for name, img in iter_source("images", args.dir):
        try:
            frame: DecodedFrame = decode_frame(img)
        except FrameRejected as e:
            discard[e.reason] += 1
            print(f"丢弃帧 {name}：[{e.reason}] {e.detail}")
            continue
        store.add(frame)

    total = 0
    if store.received_count():
        total = store.total_frames
        print(f"已收 {store.received_count()}/{total} 帧", end="")
        if discard:
            print(f"，丢弃 {sum(discard.values())} 帧（{dict(discard)}）", end="")
        print()

    if store.is_complete():
        # 豁免说明：冻结协议 §4 要求「元数据缺失不启动还原」，但本票
        # （issue #4）的发送端尚未插入元数据帧（属 issue #6），验收标准
        # 明文要求「数据帧收齐后 gunzip 落盘」。元数据门控还原随 #6 生效。
        try:
            plain = gunzip_verify(store.assemble())
        except (IncompleteError, RestoreError) as e:
            print(f"还原失败：{e}", file=sys.stderr)
            return 1
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        # 元数据帧（落盘文件名的唯一来源）属 issue #6；本票以 FILE_ID 兜底命名
        dest = out_dir / f"restored-{store.file_id:08X}"
        dest.write_bytes(plain)
        print(f"还原完成：{dest}（{len(plain)} 字节，sha256 {hashlib.sha256(plain).hexdigest()[:16]}…）")
        return 0

    print("未收齐全部数据帧，不启动还原", file=sys.stderr)
    return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="receiver", description="跨隔离网络单向文件摆渡接收端")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("receive", help="从取帧源解码还原文件")
    p.add_argument("--source", required=True, choices=["images", "desktop", "video", "camera"], help="取帧源")
    p.add_argument("--dir", type=Path, help="images 源：PNG 帧序列目录")
    p.add_argument("--video", type=Path, help="video 源：录制视频文件")
    p.add_argument("--region", help="desktop 源：捕获区域 L,T,W,H")
    p.add_argument("--out", type=Path, default=Path("output"), help="还原输出目录（默认 output/）")
    args = parser.parse_args(argv)

    try:
        startup_self_check()  # 协议 §3：两端启动时必须自检通过
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2

    if args.source == "images":
        if not args.dir:
            parser.error("--source images 需要 --dir <PNG 帧序列目录>")
        return receive_images(args)
    print(f"源 {args.source} 尚未实现（desktop: #9 / video: #10 / camera: #13）", file=sys.stderr)
    return 2
