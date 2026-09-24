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

- ~~桌面实投还有拍摄/抓屏侧的丢帧与重传轮次，本 benchmark 只覆盖「理想信道下
  的帧数规模 × 解码速率」，实投耗时 ≥ 折算值。~~ → 已由 **实投基准**（下节）补上。
- 单机解码速率与 CPU/盘相关，跨机器比较无意义，看趋势与瓶颈交叉点即可。

## 实投 desktop 闭环基准（benchmark-v2，B1 验证）

v1 的 FPS 只是信道模型折算参数，测不出抓屏丢帧。`run_bench_live.py` 全自动
实投：playwright 有头浏览器窗口播放 sender.html + mss 抓屏走 desktop 源，
单变量扫 FPS，检验 speedup-methods.md B1 的前提「desktop 抓屏实际可跑 50–60」。

```powershell
# 全量：FPS {10,15,20,30} × 5MiB × BIT=8 × 3 次接收 + 每档 1 轮 calibrate（约 35 分钟）
.venv/Scripts/python.exe benchmark/run_bench_live.py

# 冒烟（3–5 分钟，验链路）
.venv/Scripts/python.exe benchmark/run_bench_live.py --quick
```

> FPS 档位说明：原计划扫 {30,45,60} 检验 B1 上限，冒烟实测 30/60 时实收节拍仅
> 5.75/0.2 fps——稳定闸门「两帧一致」判定在抓屏周期 > 显示周期时永不满足，
> 高档位全灭。故全量向下扫，定位当前实现的实际安全上限；「抓屏可跑 50–60」
> 待 C1/C2（解码提速）与 B2（DXGI 采集）落地后复测。

**运行期间屏幕上会出现一个置顶浏览器窗口循环播放方块帧，请勿遮挡**——
抓屏采的是真实屏幕像素，遮挡即坏帧。产出 `live_desktop_results.json` / `.csv`
与 `report-live.md`。

关键口径：

| 项 | 说明 |
|---|---|
| wall_s | 文件载入（播放开始）→ 还原写盘完成，含最多一整轮循环等待 = 实投吞吐 |
| 缺帧判级 | calibrate 抓 1 轮循环（deadline 干净收尾），issue #29 三级口径 |
| 实测到达 FPS | calibrate 帧间隔中位数 + p5/p95——mss 采集跟不上的第一手证据（B2 依据） |
| 每轮新文件 | 新 file_id 新进度目录（重定向到 `results/work/progress/`），断点续传不串轮 |

## 解码专项基准（C1+C2 验证，issue #30）

单帧解码耗时的优化前后对比：几何矩阵沿用 v1 + 4K 外推档，四配置
（legacy / 仅C1 / 仅C2 / C1+C2）逐帧计时并做解码结果逐位一致性校验。

```powershell
# 全量：9 几何 × 60 帧 × 3 轮（约 2 分钟）
.venv/Scripts/python.exe benchmark/run_bench_decode.py

# 冒烟（秒级）
.venv/Scripts/python.exe benchmark/run_bench_decode.py --quick
```

产出 `decode_results.json` / `.csv` 与 [`report-decode.md`](report-decode.md)。
结论速览：C1+C2 合计 2.2–4.6×，4K 单帧 58.2 → 13.1 ms（解码 17 → 76 fps，
v1 预言的 4K 瓶颈翻转解除）。
