"""断点续传 + 参数锁定（issue #7）。

两条缝：
- 模块单测：FrameStore 公共接口（持久化 / 硬锁 / 惰性加载），参照
  tests/test_sanitize.py 的直接单测惯例（需求文档 §八「协议层单测：
  参数锁定拒绝路径」）；
- 缝 B：接收端 CLI 子进程外沿（崩溃重启续传 / 迟加入收齐 / 锁定告警
  与进度不作废），run_receive 复用 tests/test_receiver_images.py。

验收（issue #7）：
- Ctrl+C 或崩溃后重启继续接收，只补缺失帧；
- 进度持久化为位图 + 定长文件，meta.json 原子写；
- 参数锁定拒绝路径有单测，不自动作废进度；
- 循环重播下接收端任意时刻加入均可收齐。
"""

import gzip
import json
import os
import shutil
import zlib
from pathlib import Path

import pytest

import fixture_encoder
from receiver import paths
from receiver.metadata import FileMetadata
from receiver.pipeline import DecodedFrame, FrameHeader
from receiver.protocol import FRAME_NO_METADATA, FrameRejected
from receiver.store import FrameStore
from test_receiver_images import geometry, run_receive


# ---------- 模块单测夹具：直接构造 DecodedFrame，不经图像流水线 ----------

def header(frame_no, total_frames, chunk_size, *, file_id=0xDEADBEEF,
           data_len=None, cols=48, rows=48, bit=4, pad=3) -> FrameHeader:
    return FrameHeader(
        file_id=file_id, frame_no=frame_no, total_frames=total_frames,
        data_len=data_len if data_len is not None else chunk_size,
        chunk_size=chunk_size, cols=cols, rows=rows, bit=bit, pad=pad,
        flags=0x8000,
    )


def data_frame(frame_no, total_frames, chunk_size, **kw) -> DecodedFrame:
    payload = bytes([frame_no & 0xFF]) * (kw.pop("data_len", None) or chunk_size)
    return DecodedFrame(header=header(frame_no, total_frames, chunk_size, **kw),
                        payload=payload)


def meta_frame(total_frames, chunk_size, name="resume.bin", *,
               compressed_size=None, **kw) -> DecodedFrame:
    payload = bytes(range(chunk_size))  # 内容不参与收集判据，占位即可
    return DecodedFrame(
        header=header(FRAME_NO_METADATA, total_frames, chunk_size,
                      data_len=len(payload), **kw),
        payload=payload,
        metadata=FileMetadata(method=1, plain_size=1234,
                              compressed_size=(total_frames * chunk_size - 5
                                               if compressed_size is None
                                               else compressed_size),
                              name=name),
    )


def task_dir(root: Path, file_id: int) -> Path:
    return root / f"{file_id:08X}"


def export_ok(src: bytes, frames_dir: Path, filename: str, **geo) -> list:
    """按默认（或覆盖）几何导出带元数据帧的 PNG 序列。"""
    comp = gzip.compress(src)
    return fixture_encoder.export_frames(
        comp, zlib.crc32(src) & 0xFFFFFFFF, frames_dir,
        filename=filename, plain_size=len(src), **geo,
    )


# ---------- 锁定创建：首帧锁定 → 任务目录落盘（meta.json 原子写） ----------

class TestLockCreation:
    def test_first_frame_locks_params_and_writes_task_files(self, tmp_path):
        """首个数据帧落地：progress/<FILE_ID>/ 下 meta.json（fileId + 几何 +
        chunkSize + 帧总数锁定）、data.bin（total×chunk 定长）、received.bin
        （位图）齐备；原子写不留 meta.json.tmp。"""
        store = FrameStore(tmp_path)

        assert store.add(data_frame(0, total_frames=4, chunk_size=100)) is True

        tdir = task_dir(tmp_path, 0xDEADBEEF)
        meta = json.loads((tdir / "meta.json").read_text("utf-8"))
        assert meta["file_id"] == "DEADBEEF"
        assert meta["total_frames"] == 4
        assert meta["chunk_size"] == 100
        assert (meta["cols"], meta["rows"], meta["bit"], meta["pad"]) == (48, 48, 4, 3)
        assert (tdir / "data.bin").stat().st_size == 4 * 100
        assert (tdir / "received.bin").stat().st_size == 1  # ceil(4/8) 字节位图
        assert not (tdir / "meta.json.tmp").exists(), "原子写不得残留 tmp"
        store.close()

    def test_bitmap_tracks_received_frames(self, tmp_path):
        """每收一帧：data.bin 对应偏移写入 payload，位图置位，重启可读回。"""
        store = FrameStore(tmp_path)
        store.add(data_frame(0, 4, 100))
        store.add(data_frame(2, 4, 100))
        assert store.received_count() == 2
        assert store.data_complete() is False
        store.close()

        reloaded = FrameStore(tmp_path)
        # 重播命中已收帧：惰性加载由首个数据帧触发，去重返回 False
        assert reloaded.add(data_frame(0, 4, 100)) is False, "重启后已收帧应去重"
        assert reloaded.add(data_frame(2, 4, 100)) is False
        assert reloaded.received_count() == 2, "重启后惰性加载已收帧"
        assert reloaded.data_complete() is False
        reloaded.close()


# ---------- 参数锁定硬锁（协议 §6：整帧拒绝并告警，不自动作废） ----------

class TestParamLock:
    def test_geometry_mismatch_rejected_as_param_lock(self, tmp_path):
        """同 fileId、COLS 漂移 → FrameRejected('param_lock')，整帧拒绝。"""
        store = FrameStore(tmp_path)
        store.add(data_frame(0, 4, 100))

        with pytest.raises(FrameRejected) as ei:
            store.add(data_frame(1, 4, 100, cols=64))

        assert ei.value.reason == "param_lock"
        assert "COLS" in ei.value.detail
        store.close()

    def test_chunk_and_total_mismatch_rejected_as_param_lock(self, tmp_path):
        """同 fileId、chunkSize / TOTAL_FRAMES 漂移 → 同样硬锁拒绝。"""
        store = FrameStore(tmp_path)
        store.add(data_frame(0, 4, 100))

        with pytest.raises(FrameRejected) as ei_chunk:
            store.add(data_frame(1, 4, 200))
        assert ei_chunk.value.reason == "param_lock"

        with pytest.raises(FrameRejected) as ei_total:
            store.add(data_frame(1, 5, 100))
        assert ei_total.value.reason == "param_lock"
        store.close()

    def test_foreign_file_id_still_rejected_as_file_id(self, tmp_path):
        """跨任务（不同 fileId）混帧维持原 'file_id' 拒绝语义。"""
        store = FrameStore(tmp_path)
        store.add(data_frame(0, 4, 100))

        with pytest.raises(FrameRejected) as ei:
            store.add(data_frame(0, 4, 100, file_id=0xCAFEBABE))

        assert ei.value.reason == "file_id"
        store.close()

    def test_lock_rejection_does_not_invalidate_progress(self, tmp_path):
        """硬锁不自动作废：拒绝帧不改动 meta.json / data.bin / received.bin，
        参数恢复一致后续帧照常接收。"""
        store = FrameStore(tmp_path)
        store.add(data_frame(0, 4, 100))
        tdir = task_dir(tmp_path, 0xDEADBEEF)
        before = {p.name: p.read_bytes() for p in tdir.iterdir()}

        with pytest.raises(FrameRejected):
            store.add(data_frame(1, 4, 100, cols=64))
        with pytest.raises(FrameRejected):
            store.add(data_frame(1, 4, 100, file_id=0xCAFEBABE))

        after = {p.name: p.read_bytes() for p in tdir.iterdir()}
        assert after == before, "拒绝路径不得改动任何任务文件"
        assert store.add(data_frame(1, 4, 100)) is True, "参数恢复一致后续帧照常收"
        store.close()

    def test_metadata_frame_also_locked(self, tmp_path):
        """已锁定后元数据帧头参数漂移 → 同样硬锁拒绝（帧头参数一致性无例外）。"""
        store = FrameStore(tmp_path)
        store.add(data_frame(0, 4, 100))

        with pytest.raises(FrameRejected) as ei:
            store.add(meta_frame(4, 100, cols=64))

        assert ei.value.reason == "param_lock"
        store.close()

    def test_foreign_metadata_frame_not_adopted_into_new_task(self, tmp_path):
        """锁定前到达的元数据帧若属于别的 fileId，不得写进新任务 meta.json
        （否则异文件名/尺寸会毒化新任务的还原）。"""
        store = FrameStore(tmp_path)
        store.add(meta_frame(4, 100, name="别的文件.bin", file_id=0xAAA00001))

        store.add(data_frame(0, 4, 100, file_id=0xAAA00002))
        tdir = task_dir(tmp_path, 0xAAA00002)
        meta = json.loads((tdir / "meta.json").read_text("utf-8"))
        assert meta["metadata"] is None, "异 fileId 元数据不得写入新任务"

        store.add(meta_frame(4, 100, name="本尊.bin", file_id=0xAAA00002))
        meta = json.loads((tdir / "meta.json").read_text("utf-8"))
        assert meta["metadata"]["name"] == "本尊.bin", "本任务元数据随后照常收纳"
        store.close()

    def test_incomplete_task_dir_rejected_not_crash(self, tmp_path):
        """任务目录不完整（用户手动部分清空）：整帧拒绝并告警（task_corrupt），
        不得崩溃，也不得以帧头参数误锁定。"""
        store1 = FrameStore(tmp_path)
        store1.add(data_frame(0, 4, 100))
        store1.close()

        (task_dir(tmp_path, 0xDEADBEEF) / "data.bin").unlink()
        store2 = FrameStore(tmp_path)
        for _ in range(2):
            with pytest.raises(FrameRejected) as ei:
                store2.add(data_frame(1, 4, 100))
            assert ei.value.reason == "task_corrupt"
        store2.close()

        tdir = task_dir(tmp_path, 0xDEADBEEF)
        (tdir / "data.bin").write_bytes(b"\x00" * 399)  # 长度与定长容量不符
        store3 = FrameStore(tmp_path)
        with pytest.raises(FrameRejected) as ei:
            store3.add(data_frame(1, 4, 100))
        assert ei.value.reason == "task_corrupt"
        store3.close()


# ---------- 惰性加载续传（只补缺失帧）+ 元数据持久化 ----------

class TestResume:
    def test_resume_fills_only_missing_frames(self, tmp_path):
        """崩溃重启：已收帧由任务目录恢复，重播去重，缺失帧补齐后收齐。"""
        store1 = FrameStore(tmp_path)
        store1.add(data_frame(0, 4, 100))
        store1.add(data_frame(3, 4, 100))
        store1.close()

        store2 = FrameStore(tmp_path)
        assert store2.add(data_frame(0, 4, 100)) is False, "重播已收帧去重"
        assert store2.add(data_frame(2, 4, 100)) is True, "只补缺失帧"
        assert store2.add(data_frame(3, 4, 100)) is False
        assert store2.add(data_frame(1, 4, 100)) is True
        assert store2.data_complete() is True
        store2.close()

        tdir = task_dir(tmp_path, 0xDEADBEEF)
        bitmap = (tdir / "received.bin").read_bytes()
        assert bitmap == b"\xF0", f"4 帧位图 MSB first 应为 0xF0，实际 {bitmap.hex()}"

    def test_payload_content_restored_across_restart(self, tmp_path):
        """data.bin 定长写入的 payload 重启后逐字节读回（不是只记位图）。"""
        payload0 = bytes([0xAB]) * 100
        payload3 = bytes(range(100))  # 末帧故意短于 chunk：data_len=60
        comp = payload0 + b"\x11" * 100 + b"\x22" * 100 + payload3[:60]

        store1 = FrameStore(tmp_path)
        store1.add(meta_frame(4, 100, name="r.bin", compressed_size=len(comp)))
        store1.add(DecodedFrame(header=header(0, 4, 100), payload=payload0))
        store1.add(DecodedFrame(header=header(3, 4, 100, data_len=60), payload=payload3[:60]))
        store1.close()

        store2 = FrameStore(tmp_path)
        store2.add(DecodedFrame(header=header(1, 4, 100), payload=b"\x11" * 100))
        store2.add(DecodedFrame(header=header(2, 4, 100), payload=b"\x22" * 100))
        assert store2.assemble() == comp, "拼装 = 各帧 payload 按序 + 末帧截断"
        store2.close()

    def test_metadata_persisted_and_restored(self, tmp_path):
        """元数据帧写入 meta.json；重启后惰性加载还原，还原判据直接成立。"""
        store1 = FrameStore(tmp_path)
        for n in range(4):
            store1.add(data_frame(n, 4, 100))
        store1.add(meta_frame(4, 100, name="续传.bin"))
        assert store1.is_complete() is True
        store1.close()

        store2 = FrameStore(tmp_path)
        assert store2.add(data_frame(0, 4, 100)) is False  # 触发惰性加载
        assert store2.metadata is not None
        assert store2.metadata.name == "续传.bin"
        assert store2.is_complete() is True, "重启后不收任何新帧即满足还原判据"
        assert store2.assemble() is not None
        store2.close()

    def test_assemble_truncates_last_frame_by_compressed_size(self, tmp_path):
        """末帧槽位定长 chunk，拼装按 compressedSize 截断（不含补位零）。"""
        store = FrameStore(tmp_path)
        store.add(meta_frame(2, 100, name="t.bin"))  # compressed_size = 2*100-5 = 195
        for n in range(2):
            store.add(data_frame(n, 2, 100))
        comp = store.assemble()
        assert len(comp) == 195, f"末帧应截断到 95 字节，实际拼出 {len(comp)}"
        store.close()


# ---------- meta.json 落盘抗文件占用（issue #43） ----------

class TestMetaWriteResilience:
    """os.replace 撞 WinError 32（Defender / 索引器对刚高频写入的任务目录
    瞬时扫描）：有限退避重试消化，仍失败降级为告警——meta 是辅助产物，
    不得让收帧崩溃、不得覆盖传输成败。"""

    @pytest.fixture
    def locked_replace(self, monkeypatch):
        """把 store 模块视角的 os.replace / Path.write_text 换成可编程替身，
        sleep 免真实等待；failures_left 卡 replace 侧、write_failures_left
        卡 meta.json.tmp 的写入侧（meta.json 自身的写不受影响）。"""
        import receiver.store as store_mod
        real_replace = os.replace
        real_write = Path.write_text
        state = {"failures_left": 0, "write_failures_left": 0}

        def replace(src, dst):
            if state["failures_left"] > 0:
                state["failures_left"] -= 1
                raise PermissionError(32, "另一个程序正在使用此文件，进程无法访问。")
            real_replace(src, dst)

        def write_text(path, data, encoding=None):
            if state["write_failures_left"] > 0 and path.name == "meta.json.tmp":
                state["write_failures_left"] -= 1
                raise PermissionError(32, "另一个程序正在使用此文件，进程无法访问。")
            return real_write(path, data, encoding=encoding)

        monkeypatch.setattr(store_mod.os, "replace", replace)
        monkeypatch.setattr(Path, "write_text", write_text)
        monkeypatch.setattr(store_mod.time, "sleep", lambda _s: None)
        return state

    def test_write_meta_retries_through_temporary_lock(self, tmp_path, locked_replace):
        """瞬时占用在前 2 次重试内恢复 → meta.json 照常原子落盘，无告警。"""
        locked_replace["failures_left"] = 2
        store = FrameStore(tmp_path)

        store.add(data_frame(0, 4, 100))

        meta = json.loads(
            (task_dir(tmp_path, 0xDEADBEEF) / "meta.json").read_text("utf-8"))
        assert meta["total_frames"] == 4
        assert not (task_dir(tmp_path, 0xDEADBEEF) / "meta.json.tmp").exists()
        assert store.warnings == []
        store.close()

    def test_write_meta_retries_when_tmp_write_locked(self, tmp_path, locked_replace):
        """占用发生在 tmp 写入侧（不只 replace 侧）→ 同样退避重试消化，
        meta.json 照常落盘、无告警。"""
        locked_replace["write_failures_left"] = 2
        store = FrameStore(tmp_path)

        store.add(data_frame(0, 4, 100))

        meta = json.loads(
            (task_dir(tmp_path, 0xDEADBEEF) / "meta.json").read_text("utf-8"))
        assert meta["total_frames"] == 4
        assert store.warnings == []
        store.close()

    def test_write_meta_locked_persistently_degrades_to_warning(self, tmp_path, locked_replace):
        """持续占用重试耗尽 → 不抛异常、收帧照常，降级为一条告警；
        降级前清掉 tmp，不留脏文件。"""
        locked_replace["failures_left"] = 10 ** 9  # 恒失败
        store = FrameStore(tmp_path)

        assert store.add(data_frame(0, 4, 100)) is True, "meta 落盘失败不得让收帧崩溃"
        assert store.add(data_frame(1, 4, 100)) is True, "后续帧照常接收"

        tdir = task_dir(tmp_path, 0xDEADBEEF)
        assert len(store.warnings) == 1
        assert "meta.json" in store.warnings[0]
        assert "进度不保留" in store.warnings[0], "meta 缺失时重启按新任务重建，须言明"
        assert (tdir / "data.bin").stat().st_size == 400, "数据落盘不受影响（定长容量 total×chunk）"
        assert not (tdir / "meta.json.tmp").exists()
        store.close()

    def test_metadata_overwrite_failure_keeps_old_meta(self, tmp_path, locked_replace):
        """meta.json 已在盘（锁定时写成功）、其后元数据帧覆盖失败：旧 meta
        保持可用，告警不得宣称进度不保留。"""
        store = FrameStore(tmp_path)
        for n in range(4):
            store.add(data_frame(n, 4, 100))
        locked_replace["failures_left"] = 10 ** 9

        store.add(meta_frame(4, 100, name="续传.bin"))

        assert store.metadata is not None, "元数据帧照常收纳进内存"
        assert store.is_complete() is True, "本进程还原判据不受 meta 落盘失败影响"
        assert len(store.warnings) == 1
        assert "保留旧 meta" in store.warnings[0]
        old = json.loads(
            (task_dir(tmp_path, 0xDEADBEEF) / "meta.json").read_text("utf-8"))
        assert old["metadata"] is None, "旧 meta 未被失败的覆盖破坏"
        store.close()


# ---------- 缝 B：CLI 子进程外沿（崩溃重启 / 迟加入 / 锁定告警） ----------

@pytest.fixture
def progress_root():
    """缝 B 断点续传任务落在锚定 progress/，用后清理（同 test_progress_anchor）。"""
    yield paths.PROGRESS_DIR
    shutil.rmtree(paths.PROGRESS_DIR, ignore_errors=True)


class TestCliResume:
    def test_crash_restart_only_missing_frames(self, tmp_path, progress_root):
        """崩溃重启续传：首轮只收到后半段（rc=1，任务目录落盘），
        重启收到完整重播 → 只补前半段，收齐还原。"""
        src = os.urandom(2000)
        full = tmp_path / "full"
        paths_all = export_ok(src, full, "ferry.bin")
        partial = tmp_path / "partial"
        shutil.copytree(full, partial)
        for p in paths_all[: len(paths_all) // 2]:
            (partial / p.name).unlink()  # 只播后半段：模拟崩溃前已错过的帧
        out = tmp_path / "output"

        r1 = run_receive(partial, out)
        assert r1.returncode == 1, f"stdout={r1.stdout}\nstderr={r1.stderr}"
        fid = f"{zlib.crc32(src) & 0xFFFFFFFF:08X}"
        assert (progress_root / fid / "meta.json").is_file(), "任务目录应锁定落盘"

        r2 = run_receive(full, out)
        assert r2.returncode == 0, f"stdout={r2.stdout}\nstderr={r2.stderr}"
        assert (out / "ferry.bin").read_bytes() == src
        assert "已收" in r2.stdout

    def test_late_join_catches_up_on_replay(self, tmp_path, progress_root):
        """循环重播下迟加入：首轮只收到前半段，重播补齐后半段，收齐还原。"""
        src = os.urandom(2000)
        full = tmp_path / "full"
        paths_all = export_ok(src, full, "ferry.bin")
        partial = tmp_path / "partial"
        shutil.copytree(full, partial)
        for p in paths_all[len(paths_all) // 2 :]:
            (partial / p.name).unlink()  # 只播前半段：模拟接收端中途才加入
        out = tmp_path / "output"

        assert run_receive(partial, out).returncode == 1
        r2 = run_receive(full, out)
        assert r2.returncode == 0, f"stdout={r2.stdout}\nstderr={r2.stderr}"
        assert (out / "ferry.bin").read_bytes() == src

    def test_param_lock_alert_and_progress_kept(self, tmp_path, progress_root):
        """同 fileId 参数漂移：CLI 告警（param_lock）拒绝整帧，退出码 1；
        旧进度不作废——换回原参数重播后照常收齐还原。"""
        src = os.urandom(800)
        original = tmp_path / "original"
        paths_all = export_ok(src, original, "lock.bin")
        replay = tmp_path / "replay"
        shutil.copytree(original, replay)  # 完整重播目录（run3 用）
        drifted = tmp_path / "drifted"
        export_ok(src, drifted, "lock.bin", cols=64)  # 同内容 → 同 fileId，几何漂移
        out = tmp_path / "output"

        # run1 只播前半段：锁定但不收齐（若已收齐，漂移帧拒绝后仍会还原已完成任务）
        for p in paths_all[len(paths_all) // 2 :]:
            (original / p.name).unlink()
        assert run_receive(original, out).returncode == 1
        fid = f"{zlib.crc32(src) & 0xFFFFFFFF:08X}"
        meta_before = (progress_root / fid / "meta.json").read_bytes()

        r2 = run_receive(drifted, out)
        assert r2.returncode == 1, f"stdout={r2.stdout}\nstderr={r2.stderr}"
        assert "param_lock" in r2.stdout, f"应告警参数锁定：{r2.stdout}"
        assert (progress_root / fid / "meta.json").read_bytes() == meta_before, "进度不得被作废"

        r3 = run_receive(replay, out)
        assert r3.returncode == 0, f"换回原参数应续传完成：stdout={r3.stdout}\nstderr={r3.stderr}"
        assert (out / "lock.bin").read_bytes() == src
