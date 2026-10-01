"""稳定闸门精确比较微基准；默认采用 receive_20261001T172857 的采集尺寸。

运行：.venv/Scripts/python.exe benchmark/bench_stable_gate.py
数字仅代表比较函数耗时，不代表实际传输耗时。
"""

from pathlib import Path
import statistics
import sys
import time

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from receiver.sources.stable import StableFrameGate


def original_same(a, b, tol=2, frac_max=5e-4):
    if a.shape != b.shape:
        return False
    diff = np.abs(a.astype(np.int16) - b.astype(np.int16))
    return bool((diff > tol).mean() <= frac_max)


def measure(compare, a, b):
    for _ in range(10):
        compare(a, b)
    samples = []
    for _ in range(100):
        started = time.perf_counter()
        compare(a, b)
        samples.append((time.perf_counter() - started) * 1000)
    return statistics.median(samples), sorted(samples)[94]


def main():
    rng = np.random.default_rng(0)
    a = rng.integers(0, 256, (769, 1856), dtype=np.uint8)
    sparse = a.copy()
    sparse.flat[:700] = 255 - sparse.flat[:700]
    cases = [("same", a, a.copy()), ("sparse-noise", a, sparse),
             ("changed", a, 255 - a),
             ("strided", a[::2, ::2], a.copy()[::2, ::2])]
    print("case          original median/p95 ms  optimized median/p95 ms  speedup")
    for name, left, right in cases:
        gate = StableFrameGate()
        assert gate._same(left, right) == original_same(left, right), name
        old, old_p95 = measure(original_same, left, right)
        new, new_p95 = measure(gate._same, left, right)
        print(f"{name:13} {old:7.3f}/{old_p95:7.3f}"
              f"           {new:7.3f}/{new_p95:7.3f}        {old / new:5.1f}x")


if __name__ == "__main__":
    main()
