# 参数矩阵 benchmark

测量「窗口面积 × BIT × FPS」对文件摆渡接收耗时的影响。纯离线闭环：
不发屏幕、不占摄像头，帧序列落盘 PNG 后走 images 源全速解码，结果可复现。

## 运行

```powershell
# 完整矩阵：4 文件大小 × 3 窗口 × 3 BIT × 3 FPS（约 5–10 分钟）
.venv/Scripts/python.exe benchmark/run_bench.py

# 冒烟（秒级，只验脚本可用）
.venv/Scripts/python.exe benchmark/run_bench.py --quick
```

产出（`benchmark/results/`）：

- `benchmark_results.json` — 全量结果（含采样运行的原始样本数据）
- `benchmark_results.csv` — 长表（每组合 × FPS 一行），可直接贴 Excel 分析
- `report.md` — 结果与关系分析

辅助：`analyze.py` — 从 `benchmark_results.json` 提取关键数字做关系验证
（BIT²/面积/帧数比等），改矩阵后重跑可复算报告结论。

## 口径

| 项 | 说明 |
|---|---|
| 几何推导 | 与 sender.html `computeGeometry()` 同式（`docs` 冻结项），dpr=1；`(COLS×ROWS) mod 8` 不满足时下调 ROWS（发送端对该冻结约束无守卫，benchmark 不复刻该缺陷） |
| 帧序列 | `tests/fixture_encoder.py`（协议独立实现）封帧 + numpy 向量化渲染，元数据帧节奏与发送端一致（每 100 数据帧前一帧） |
| 负载 | `os.urandom`（不可压缩，gzip 后 ≈ 原长，worst case） |
| 接收耗时 | `receiver.run.run_receive` 的 `elapsed`：首个数据帧落地 → 还原写盘完成，images 源全速（无播放节奏） |
| FPS 折算 | FPS 是发送端播放参数，不影响解码耗时。信道模型：`总耗时 = max(导出帧数/FPS, 解帧耗时)`，瓶颈字段标注 `playback` / `receiver` |
| 采样外推 | 数据帧数 > 4000 的组合（如 20MiB × BIT=12 ≈ 7 万帧）不渲染全量，实测 120 帧取单帧耗时线性外推，`sampled=1` 标记 |

## 已知边界

- 桌面实投还有拍摄/抓屏侧的丢帧与重传轮次，本 benchmark 只覆盖「理想信道下
  的帧数规模 × 解码速率」，实投耗时 ≥ 折算值。
- 单机解码速率与 CPU/盘相关，跨机器比较无意义，看趋势与瓶颈交叉点即可。
