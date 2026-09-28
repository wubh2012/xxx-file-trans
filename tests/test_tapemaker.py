"""制带工具测试（issue #38，需求文档 §11）。

三层缝：
  - 帧序列布局（元数据节奏 / FEC 插入 / 轮次口径）与协议上限拦截；
  - 渲染位图直接过 receiver.pipeline.decode_frame（角标几何自举 + CRC
    全链路，receiver.protocol 作 CRC 仲裁）；
  - 制带出片 → ffmpeg 解码回帧序列 → receiver.run.run_receive 还原，
    sha256 与源文件一致（§11.4.1 往返闭环的缝内版；经 video 源完整
    闭环见 issue #39——稳定闸门与带的适配另立 issue）。

未安装 ffmpeg 时出片相关用例自动跳过。
"""

import hashlib
import shutil
from pathlib import Path

import cv2
import numpy as np
import pytest

from receiver.fec import recover_two
from receiver.pipeline import GeometryCache, decode_frame
from receiver.protocol import (
    FEC_GROUP_SIZE,
    FLAGS_FEC,
    FRAME_NO_METADATA,
    verify_crc,
)
from receiver.run import run_receive
from tapemaker.cli import main
from tapemaker.frames import (
    DEFAULT_BIT,
    RESOLUTIONS,
    build_round,
    derive_geometry,
)
from tapemaker.render import render_frame

TEST_GEO = derive_geometry(960, 540, bit=4, pad=3)  # 小网格：单测快


def _payload(n: int, seed: int = 7) -> bytes:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, n, dtype=np.uint8).tobytes()


# ---------- 几何推导 ----------

def test_derive_geometry_1080p_default_bit():
    """1080p 默认 BIT=8（BIT=6 会推出 COLS=314 超帧头 1 字节上限）；
    (COLS×ROWS) mod 8 == 0 冻结约束靠递减 ROWS 满足（与 sender.html
    deriveGeometry 同口径）。"""
    geo = derive_geometry(1920, 1080, bit=8, pad=3)
    assert geo.cols == 234 and geo.rows == 128
    assert (geo.cols * geo.rows) % 8 == 0
    assert geo.canvas_h - 2 * geo.pad * geo.bit >= geo.rows * geo.bit  # 网格在画布内


def test_derive_geometry_4k_default_bit_within_header_limit():
    """4K 默认 BIT=15：COLS/ROWS 均不超帧头 1 字节上限（≤255）。"""
    geo = derive_geometry(*RESOLUTIONS["4k"], bit=DEFAULT_BIT["4k"], pad=3)
    assert geo.cols <= 255 and geo.rows <= 255
    assert (geo.cols * geo.rows) % 8 == 0


def test_derive_geometry_rejects_out_of_range():
    with pytest.raises(ValueError, match="4 bit"):
        derive_geometry(1920, 1080, bit=16, pad=3)
    with pytest.raises(ValueError, match="1 字节"):
        derive_geometry(3840, 2160, bit=4, pad=3)  # COLS 954 > 255
    with pytest.raises(ValueError, match="角标"):
        derive_geometry(1920, 1080, bit=8, pad=2)  # PAD < 3 放不下角标


# ---------- 帧序列布局 ----------

def _frame_nos(frames):
    return [f.frame_no for f in frames]


def test_round_layout_metadata_pace_and_parity_interleave():
    """一轮序列 = sender.html 播放序列口径：元数据帧在数据帧 0 / 100 / …
    之前插入；每 32 数据帧后紧跟 2 个校验帧（帧号从 TOTAL_FRAMES 起连续）；
    末组按实际帧数收尾。"""
    chunk = TEST_GEO.chunk_size
    total = 101  # 跨两个元数据插入点（0、100）+ 3 组 FEC（32/32/32/5）
    frames = build_round(_payload(chunk * total), 0xDEADBEEF, "t.bin", chunk * total, TEST_GEO)

    nos = _frame_nos(frames)
    assert nos[0] == FRAME_NO_METADATA, "每轮首帧为元数据帧"
    assert nos.count(FRAME_NO_METADATA) == 2
    second_meta_pos = nos.index(FRAME_NO_METADATA, 1)
    assert nos[second_meta_pos + 1] == 100, "第二个元数据帧紧跟在数据帧 100 之前"

    # 校验帧：位置与帧号
    parity_nos = [n for n in nos if n != FRAME_NO_METADATA and n >= total]
    assert parity_nos == [total + i for i in range(4 * 2)], "4 组 × 2 校验帧，紧跟组尾连续编号"

    # 每组 32 数据帧后紧跟 2 校验帧：抽取第一组验证相对顺序
    first_parity_pos = nos.index(total)
    assert nos[first_parity_pos - 1] == 31, "校验帧紧跟第 32 个数据帧"
    assert nos[first_parity_pos + 1] == total + 1


def test_round_frames_all_crc_valid_and_header_flags():
    """全部帧过 receiver.protocol.verify_crc（读取侧作仲裁，防写侧 CRC 口径漂移）；
    数据/元数据帧 FLAGS = gzip，校验帧 FLAGS = gzip | FEC。"""
    chunk = TEST_GEO.chunk_size
    frames = build_round(_payload(chunk * 40), 0x12345678, "t.bin", chunk * 40, TEST_GEO)
    for f in frames:
        verify_crc(f.header, f.data)  # 失败抛 FrameRejected
        flags = int.from_bytes(f.header[20:22], "big")
        if f.frame_no != FRAME_NO_METADATA and f.frame_no >= 40:
            assert flags & FLAGS_FEC, "校验帧须带 FEC 标志"
        else:
            assert not flags & FLAGS_FEC


def test_parity_recovers_two_missing_frames_with_receiver_fec():
    """抽掉同组 2 个数据分片，receiver.fec.recover_two 用 P0/P1 恢复——
    制带侧乘法表与接收端恢复必须是同一 GF 语义。"""
    chunk = TEST_GEO.chunk_size
    total = FEC_GROUP_SIZE  # 恰好一组
    payload = _payload(chunk * total)
    frames = build_round(payload, 1, "t.bin", len(payload), TEST_GEO)
    parity = [f for f in frames if f.frame_no != FRAME_NO_METADATA and f.frame_no >= total]
    data = {f.frame_no: f.data for f in frames if f.frame_no < total}

    known = [data.get(i) for i in range(total)]
    known[3] = known[17] = None
    a, b = recover_two(parity[0].data, parity[1].data, known, (3, 17))
    restored = dict(enumerate(known))
    restored[3], restored[17] = a, b
    for i in range(total):
        assert restored[i] == payload[i * chunk : (i + 1) * chunk].ljust(chunk, b"\x00")


def test_filename_too_long_rejected():
    chunk = TEST_GEO.chunk_size
    with pytest.raises(ValueError, match="文件名过长"):
        build_round(_payload(chunk), 1, "x" * (chunk + 1) + ".bin", chunk, TEST_GEO)


# ---------- 渲染 → 接收端流水线（几何自举全链路）----------

def _decode(img: np.ndarray):
    return decode_frame(img, GeometryCache())


def test_rendered_data_frame_decodes_via_receiver_pipeline():
    """渲染位图直接过 receiver 流水线：角标自举 → 交叉校验 → CRC 全通过，
    帧号与数据逐字节一致。"""
    chunk = TEST_GEO.chunk_size
    frames = build_round(_payload(chunk * 3), 0xCAFEF00D, "t.bin", chunk * 3, TEST_GEO)
    data_frame = next(f for f in frames if f.frame_no == 2)
    decoded = _decode(render_frame(data_frame, TEST_GEO))
    assert decoded.header.frame_no == 2
    assert decoded.payload == data_frame.data[: decoded.header.data_len]


def test_rendered_metadata_frame_decodes_via_receiver_pipeline():
    frames = build_round(b"\x01\x02\x03", 0xCAFEF00D, "报告final.bin", 3, TEST_GEO)
    meta = frames[0]
    assert meta.frame_no == FRAME_NO_METADATA
    decoded = _decode(render_frame(meta, TEST_GEO))
    assert decoded.metadata is not None
    assert decoded.metadata.name == "报告final.bin"


# ---------- 出片 → 解码 → 还原（往返闭环，需 ffmpeg）----------

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="未安装 ffmpeg，跳过出片往返测试"
)


@needs_ffmpeg
def test_make_roundtrip_restore_sha256(tmp_path):
    """制带出片 → cv2 逐帧解码 → receiver.run.run_receive 还原，
    sha256 与源文件一致。直接喂解码帧（旁路稳定闸门——闸门要求每传输帧
    连续重复 ≥2 次，与带「每帧恰一次」的形态冲突，适配另立 issue 跟踪）。"""
    src = tmp_path / "报告 测试#1.bin"
    src_bytes = _payload(80_000, seed=3)  # gzip 后多帧，覆盖 FEC 分组
    src.write_bytes(src_bytes)
    out_mp4 = tmp_path / "tape.mp4"
    assert main(["make", str(src), "-o", str(out_mp4)]) == 0
    assert out_mp4.stat().st_size > 0

    cap = cv2.VideoCapture(str(out_mp4))

    def frames():
        i = 0
        while True:
            ok, img = cap.read()
            if not ok:
                return
            yield f"video-{i:06d}", cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            i += 1

    result = run_receive(frames(), tmp_path / "out")
    cap.release()
    assert result.code == 0, f"还原失败：{result.error or result.incomplete}"
    assert result.sha256 == hashlib.sha256(src_bytes).hexdigest()
    assert result.name == "报告 测试#1.bin"


@needs_ffmpeg
def test_make_roundtrip_larger_file_fec_and_rounds(tmp_path):
    """数十帧量级：FEC 分组、元数据每 100 帧节奏、轮次重复全走一遍。"""
    src = tmp_path / "big.bin"
    src_bytes = _payload(60_000, seed=11)  # gzip 后 ~几十帧
    src.write_bytes(src_bytes)
    out_mp4 = tmp_path / "tape.mp4"
    assert main(["make", str(src), "-o", str(out_mp4), "--rounds", "2"]) == 0

    cap = cv2.VideoCapture(str(out_mp4))

    def frames():
        while True:
            ok, img = cap.read()
            if not ok:
                return
            yield "video", cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    result = run_receive(frames(), tmp_path / "out")
    cap.release()
    assert result.code == 0, f"还原失败：{result.error or result.incomplete}"
    assert result.sha256 == hashlib.sha256(src_bytes).hexdigest()
    assert result.received == result.total  # 收齐判据：数据帧齐


@needs_ffmpeg
def test_make_roundtrip_via_receiver_video_source(tmp_path):
    """§11.4.1 完整链路（issue #39）：tapemaker 制带 → receiver video 源
    （带模式旁路稳定闸门，issue #45）→ run_receive 还原，sha256 一致。"""
    from receiver.sources.video import iter_video

    src = tmp_path / "链路验证.bin"
    src_bytes = _payload(30_000, seed=5)
    src.write_bytes(src_bytes)
    out_mp4 = tmp_path / "tape.mp4"
    assert main(["make", str(src), "-o", str(out_mp4)]) == 0

    result = run_receive(iter_video(out_mp4, tape=True), tmp_path / "out")
    assert result.code == 0, f"还原失败：{result.error or result.incomplete}"
    assert result.sha256 == hashlib.sha256(src_bytes).hexdigest()


def test_make_missing_source_exits(tmp_path):
    with pytest.raises(SystemExit, match="源文件不存在"):
        main(["make", str(tmp_path / "nope.bin"), "-o", str(tmp_path / "t.mp4")])


def test_make_without_ffmpeg_hints_install(monkeypatch, tmp_path):
    """ffmpeg 缺失：报错提示安装方式（需求 §11.2），不静默。"""
    import tapemaker.render as render_mod

    monkeypatch.setattr(render_mod.shutil, "which", lambda _: None)
    src = tmp_path / "a.bin"
    src.write_bytes(b"x")
    with pytest.raises(RuntimeError, match="ffmpeg"):
        main(["make", str(src), "-o", str(tmp_path / "t.mp4")])
