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
# 全量：FPS {10,15,20,30} × 5MiB × CSS BIT=6/PAD=3 × 3 次接收 + 每档 1 轮 calibrate
.venv/Scripts/python.exe benchmark/run_bench_live.py

# 冒烟（3–5 分钟，验链路）
.venv/Scripts/python.exe benchmark/run_bench_live.py --quick

# B2 复测口径：dxgi 采集 + 自定义档位（结果写 *_dxgi.json/.csv）
.venv/Scripts/python.exe benchmark/run_bench_live.py --capture dxgi --fps-list 15,20,30,45,60
```

`--bit-css` 和 `--pad` 可显式覆盖参数；默认 CSS BIT=6、PAD=3，125% 显示缩放
下物理 BIT 约为 8，适合作为 desktop 吞吐基线。

### 真实文件屏幕端到端（推荐用于优化前后对比）

指定 `--input` 后，基准不再生成随机文件，而是把同一个真实文件交给
`sender.html`，打开**有头浏览器**循环播放；接收端在另一线程使用
`desktop` 取帧源（`dxgi` 或 `mss`）读取真实屏幕。每个档位同时记录校准缺帧、
实测到达 FPS、墙钟耗时、拒帧数，并要求还原结果 SHA-256 与源文件一致。
同一文件多轮复测会隔离 progress 目录，不会把上一轮断点续传混入下一轮。

```powershell
# 图片目录中的真实文件，扫 15/20/30 FPS，每档 1 次，优先 DXGI
.venv/Scripts/python.exe benchmark/run_bench_live.py `
  --capture dxgi `
  --input "C:\Users\GMKMIX\Pictures\Screenshots\屏幕截图 2026-08-17 193931.png" `
  --fps-list 15,20,30 --reps 1 --calib-cycles 1
```

需要定位尾部停顿时，在相同命令上加 `--trace`。每次正式接收会在
`benchmark/results/work/traces/` 写一份 JSONL：逐帧编号与新/重复状态、帧间
间隔、解码/落盘耗时；90/95/99/100% 检查点的缺帧号；采集队列空等心跳；以及
拼接、gunzip 校验、输出写盘、SHA-256 和进度清理各阶段耗时。日志不记录帧图或
文件内容，路径会打印在控制台并写入结果 JSON 的 `trace` 字段。
预计单轮耗时很长时可再加 `--skip-calibration`，避免校准先额外播放完整周期。
若发送端浏览器被桌面上的其他窗口遮挡，可加 `--focus-grace-seconds 15`，在抓屏
自检前留出时间把发送端窗口置前。

接收默认允许最多 12 个名义播放周期（`--receive-timeout-cycles` 可调），以覆盖
738 秒这类长尾复现；周期估算包含数据帧、每 100 帧元数据、FEC 帧和末帧 1 秒停留。

日志可用内置分析器汇总：

```powershell
.venv/Scripts/python.exe benchmark/analyze_receive_trace.py `
  "benchmark/results/work/traces/receive_dxgi_20_0_YYYYMMDDTHHMMSS.jsonl"
```

分析器输出重复播放、尾部缺帧、采集空等、解码/存储时延，以及还原各阶段耗时，
用于区分“接收端还在等缺帧”与“收齐后解压/写盘慢”。

结果写入 `live_desktop_results_dxgi_output.json/.csv`，其中 `sha_ok=true` 是
端到端通过的硬条件；`wall_s` 才是用户实际等待时间，`measuredFps` 和
`missingRate` 用于判断丢帧/重播造成的额外成本。运行时浏览器窗口必须保持
在前台且不可被遮挡，显示器不能自动熄屏。

> FPS 档位说明：原计划扫 {30,45,60} 检验 B1 上限，冒烟实测 30/60 时实收节拍仅
> 5.75/0.2 fps——稳定闸门「两帧一致」判定在抓屏周期 > 显示周期时永不满足，
> 高档位全灭。故全量向下扫，定位当前实现的实际安全上限；「抓屏可跑 50–60」
> 待 C1/C2（解码提速）与 B2（DXGI 采集）落地后复测。

**运行期间屏幕上会出现一个置顶浏览器窗口循环播放方块帧，请勿遮挡**——
抓屏采的是真实屏幕像素，遮挡即坏帧。产出 `live_desktop_results.json` / `.csv`
与 `report-live.md`。

### B2 复测（C1+C2+B2 落地后，issue #31）

2026-09-25 用 `--capture dxgi` 复测 {15,20,30,45,60}：实投安全上限
**15 → 30（翻倍）**，30fps 档 3 rep 全 OK（wall 135.9s ≈ 2.2 MiB/min）；
45/60 超出「抓屏 + 稳定闸门 + 解码」串行周期（~30–35ms）决定的 ~30fps
吞吐天花板，失败形态为几何尾部收不齐（差 6–11 帧 / 145+ 帧）。详见
[report-live.md 的 B2 复测章节](report-live.md)。数据：
`live_desktop_results_dxgi.json` / `.csv`。

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
