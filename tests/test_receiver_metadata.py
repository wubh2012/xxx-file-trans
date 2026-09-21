"""元数据帧 + 文件名七步净化端到端（issue #6，缝 B：接收端 CLI 外沿）。

只测外部行为：CLI 退出码、终端输出、output/ 落盘结果。不 mock 流水线
内部。夹具为 tests/fixture_encoder.py（协议独立实现）。

验收（issue #6）：
- 元数据帧每轮首帧 + 每 100 帧插入（发送端节奏，夹具按同一节奏造帧）；
- 还原文件名与源文件名一致，且永远不出 output/；
- 元数据缺失时数据帧照常收下、不启动还原；
- 12 + nameLen 超出 CHUNK_SIZE 的元数据帧整帧丢弃并告警，不影响数据帧。
"""

import gzip
import hashlib
import os
import shutil
import tempfile
import zlib
from pathlib import Path

import fixture_encoder
from test_receiver_images import geometry, run_receive


def test_roundtrip_restores_source_filename(tmp_path):
    """切片 1：带元数据帧的 PNG 序列 → CLI 还原出源文件名，sha256 一致。"""
    src = (b"named ferry payload, " * 30)[:555]
    name = "季度报表.txt"
    comp = gzip.compress(src)
    frames = tmp_path / "frames"
    fixture_encoder.export_frames(
        comp, zlib.crc32(src) & 0xFFFFFFFF, frames,
        filename=name, plain_size=len(src), **geometry(),
    )
    out = tmp_path / "output"

    r = run_receive(frames, out)

    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    dest = out / name
    assert dest.is_file(), f"应以源文件名落盘，实际 output/ 内容：{list(out.iterdir())}"
    assert hashlib.sha256(dest.read_bytes()).digest() == hashlib.sha256(src).digest()


def test_traversal_filename_stays_inside_output(tmp_path):
    """切片 2a：路径穿越名 ../../evil.txt → 净化为 evil.txt，落盘不出 output/。"""
    src = b"evil payload"
    comp = gzip.compress(src)
    frames = tmp_path / "frames"
    fixture_encoder.export_frames(
        comp, zlib.crc32(src) & 0xFFFFFFFF, frames,
        filename="../../evil.txt", plain_size=len(src), **geometry(),
    )
    out = tmp_path / "output"

    r = run_receive(frames, out)

    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert (out / "evil.txt").is_file(), f"应净化为 evil.txt：{list(out.iterdir()) if out.exists() else '未落盘'}"
    # 落盘永远不出 output/：output/ 之外（tmp_path 层级）不得出现 evil.txt
    assert not (tmp_path / "evil.txt").exists()


def test_backslash_traversal_filename_stays_inside_output(tmp_path):
    """切片 2b：..\\..\\x → 净化为 x。"""
    src = b"backslash payload"
    comp = gzip.compress(src)
    frames = tmp_path / "frames"
    fixture_encoder.export_frames(
        comp, zlib.crc32(src) & 0xFFFFFFFF, frames,
        filename="..\\..\\x", plain_size=len(src), **geometry(),
    )
    out = tmp_path / "output"

    r = run_receive(frames, out)

    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert (out / "x").is_file()


def test_reserved_name_sanitized(tmp_path):
    """切片 2c：CON → _CON，仍落在 output/ 内。"""
    src = b"reserved name payload"
    comp = gzip.compress(src)
    frames = tmp_path / "frames"
    fixture_encoder.export_frames(
        comp, zlib.crc32(src) & 0xFFFFFFFF, frames,
        filename="CON", plain_size=len(src), **geometry(),
    )
    out = tmp_path / "output"

    r = run_receive(frames, out)

    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    names = [p.name for p in out.iterdir()]
    assert names == ["_CON"], f"应净化为 _CON：{names}"


def test_200_byte_name_boundary():
    """切片 2d：恰好 200 UTF-8 字节的文件名完整保留。

    Windows 未启用长路径时，200 字节名叠加 pytest 深层临时目录
    （pytest-of-<user>/pytest-<n>/test_...0/）会超 MAX_PATH（issue #14），
    故本用例不用 tmp_path，改在 %TEMP% 根下建浅层工作目录。名称边界
    语义（200 字节完整保留）不变。
    """
    src = b"long name payload"
    name = "n" * 197 + "中"  # 197×1 + 3 = 200 字节
    assert len(name.encode("utf-8")) == 200
    work = Path(tempfile.mkdtemp(prefix="ferry_"))
    try:
        comp = gzip.compress(src)
        frames = work / "frames"
        fixture_encoder.export_frames(
            comp, zlib.crc32(src) & 0xFFFFFFFF, frames,
            filename=name, plain_size=len(src), **geometry(),
        )
        out = work / "output"

        r = run_receive(frames, out)

        assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
        assert (out / name).is_file(), f"200 字节名应完整保留：{[p.name for p in out.iterdir()]}"
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_midstream_metadata_over_100_frames(tmp_path):
    """切片 5：>100 数据帧 → 第 100 数据帧前也有元数据帧插入
    （每轮首帧 + 每 100 帧节奏），接收端正确收纳、拼装还原不受影响。"""
    src = os.urandom(40000)  # 随机数据 gzip 后仍 ≈40KB → 约 153 帧
    comp = gzip.compress(src)
    chunk = fixture_encoder.frame_capacity(cols=48, rows=48)
    total = (len(comp) + chunk - 1) // chunk
    assert total > 100, "前置失效：应超过 100 数据帧以触发中途插入"
    frames = tmp_path / "frames"
    paths = fixture_encoder.export_frames(
        comp, zlib.crc32(src) & 0xFFFFFFFF, frames,
        filename="midstream.bin", plain_size=len(src), **geometry(),
    )
    # 夹具节奏：每 100 数据帧前插一帧元数据 → 帧图总数 = 数据帧 + ⌈数据帧/100⌉
    import math

    assert len(paths) == total + math.ceil(total / 100)
    out = tmp_path / "output"

    r = run_receive(frames, out)

    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    dest = out / "midstream.bin"
    assert dest.is_file()
    assert hashlib.sha256(dest.read_bytes()).digest() == hashlib.sha256(src).digest()


def test_duplicate_metadata_frames_ignored(tmp_path):
    """切片 6：无限重播每轮重发元数据帧 → 重复帧静默忽略，仍恰好还原一份。"""
    src = b"replay payload"
    comp = gzip.compress(src)
    frames = tmp_path / "frames"
    fixture_encoder.export_frames(
        comp, zlib.crc32(src) & 0xFFFFFFFF, frames,
        filename="replay.bin", plain_size=len(src), **geometry(),
    )
    shutil.copyfile(frames / "000001.png", frames / "000000.png")  # 再投一份相同元数据帧
    out = tmp_path / "output"

    r = run_receive(frames, out)

    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    files = list(out.iterdir())
    assert files == [out / "replay.bin"], f"应恰好还原一份：{files}"
    assert files[0].read_bytes() == src


def test_metadata_missing_data_kept_no_restore(tmp_path):
    """切片 3：屏蔽元数据帧 → 数据帧照常收下（输出有已收计数），
    但不启动还原、不落盘，非零退出（验收标准 8）。"""
    src = os.urandom(3000)
    comp = gzip.compress(src)
    frames = tmp_path / "frames"
    fixture_encoder.export_frames(
        comp, zlib.crc32(src) & 0xFFFFFFFF, frames,
        filename=None,  # 不带元数据帧
        **geometry(),
    )
    out = tmp_path / "output"

    r = run_receive(frames, out)

    assert r.returncode == 1, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert "元数据" in r.stdout + r.stderr
    assert not out.exists() or not any(out.iterdir()), "元数据缺失不得还原落盘"


def test_oversized_metadata_frame_discarded_and_warned(tmp_path):
    """切片 4：12 + nameLen 超出 CHUNK_SIZE 的元数据帧整帧丢弃并告警，
    不影响数据帧接收（验收标准 10）。

    超长元数据帧无法物理渲染（数据区装不下真实文件名），按夹具约定
    手搓：nameLen 字段谎报 300，数据区仅含 12 字节固定段。
    """
    src = b"oversized metadata payload"
    comp = gzip.compress(src)
    fid = zlib.crc32(src) & 0xFFFFFFFF
    frames = tmp_path / "frames"
    paths = fixture_encoder.export_frames(
        comp, fid, frames, filename=None, **geometry(),  # 数据帧不带元数据
    )
    chunk = fixture_encoder.frame_capacity(cols=48, rows=48)
    total = (len(comp) + chunk - 1) // chunk
    # 手搓 nameLen=300 的元数据帧（12 + 300 = 312 > CHUNK_SIZE 262），排在最前
    mp = (
        b"\x01"
        + len(src).to_bytes(4, "big")
        + len(comp).to_bytes(4, "big")
        + (300).to_bytes(2, "big")
        + b"\x00"
    )
    header = fixture_encoder.signed_header(fid, 0xFFFFFF, total, mp, chunk, **geometry())
    fixture_encoder.render_png(header, mp, geometry()["bit"], geometry()["pad"], frames / "000000.png")
    assert len(paths) == total
    out = tmp_path / "output"

    r = run_receive(frames, out)

    assert r.returncode == 1, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert "metadata" in r.stdout, f"超长元数据帧应被丢弃并告警：{r.stdout}"
    assert not out.exists() or not any(out.iterdir()), "元数据被丢弃后不得还原"
