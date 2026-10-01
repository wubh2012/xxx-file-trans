# 红熊猫 JPG 优化后真实桌面传输测试

日期：2026-10-01（Asia/Singapore）。

文件未经缩放或重新压缩：3,567,634 字节；SHA-256 `84bd755492ce6a23b349cc8127dd44322e84cba25480d288b78f1b61580b8d74`。
输入副本：`benchmark/fixtures/red-panda.jpg`。

## 结果

| 发送 FPS | 从文件载入至接收任务结束（秒） | 拒帧次数 | 队列丢帧 | SHA-256 |
|---|---:|---:|---:|---|
| 20 | 81.110 | 6 | 0 | 一致 |
| 30 | 54.103 | 2 | 0 | 一致 |
| 30 | 54.127 | 3 | 0 | 一致 |
| 30 | 54.102 | 2 | 0 | 一致 |

30 FPS 三次中位耗时 54.103 秒。
所有测试直接收到全部 1,468 个数据帧，未等第二轮补帧，也未需要 FEC 恢复。
从开始载入文件计时，包含 gzip/启动播放等待与接收收尾，较单纯解码计时更完整。

## 条件与比较边界

有头 Chromium 实际窗口播放，DXGI 真实桌面采集，使用生产 `run_receive` 核心。
CSS BIT=6，实测物理 BIT=8，PAD=3，216×91 格；每数据帧 2,431 字节。
窗口 1536×816 DIP，DPR=1.25；采集区域 1920×976 物理像素。
采集器就绪后才加载文件；每次进度目录独立，无旧进度；测试保持画面无遮挡。
GUI EXE 已打包；时间来自相同核心的实投基准，未通过 tkinter 手工点选计时。

之前三份用户日志有 4,466 个数据帧，本次只有 1,468 个：几何/容量不同，
且之前存在遮挡和补帧等待，不能把 314–418 秒到约 54 秒的变化全部归因于代码。
30 FPS 后两次为独立确认测试，其中最后一次与 EXE 打包部分并行。

## 本次实现

- 已通过帧头及 CRC 验证的几何才可进入快速路径。
- 缓存采样坐标，固定阈值快速采样；失败回退 Otsu、角标定位和完整邻域采样。
- 同尺寸画面仅角标局部遮挡时，可尝试已验证坐标；数据被挡仍按 CRC 拒绝。
- P0 丢失时支持 P1 恢复一个缺失数据分片；两个缺片仍需两份校验。
- 窗口播放以最多 64 个 FEC 组交错，元数据每 100 个数据帧插入一次；
  相同帧集合与字节格式，PNG 导出及全屏播放保持原顺序。
- 采集就绪回调和 GUI 提示；基准等待实际就绪而非固定 sleep。
- 日志分析器区分交错顺序，避免误报轮次。
- 保留此前稳定比较缓冲区复用及 DXGI 120 Hz 读取节奏。

交错的保护范围受当前窗口组数和丢帧长度限制，不能保证任意长时间遮挡都可恢复。
固定 FEC 分组和校验数量未改变；没有添加反向通道或跳过完整性验证。

## 验证

全量测试 357 passed，1 deselected（已有 CLI 后端断言与当前实现不一致，原比较实现也失败）。
最后补充采集就绪、首次 CRC 失败不激活缓存、交错日志分析测试后，相关测试 65 passed，1 deselected。
四份接收输出再次逐字节与原 JPG 比对，全部一致。

## 复现

```powershell
.venv/Scripts/python.exe benchmark/run_bench_live.py --capture dxgi --input benchmark/fixtures/red-panda.jpg --fps-list 20,30 --bit-css 6 --reps 1 --skip-calibration --trace --run-label new-comparison --receive-timeout-cycles 2
```

使用 `dist/optimized-final/receiver_gui.exe` 配合该目录的 `sender.html`；
发送端选择窗口播放，BIT 输入 6，PAD 3，FPS 30，确认实际几何与本报告一致。
输出目录及日志目录锚定 EXE 所在目录。

## 原始日志

- `benchmark/results/work/panda-optimized-20261001/traces/receive_dxgi_20_0_20261001T110716.jsonl`
- `benchmark/results/work/panda-optimized-20261001/traces/receive_dxgi_30_0_20261001T110837.jsonl`
- `benchmark/results/work/panda-confirm-20261001/traces/receive_dxgi_30_0_20261001T111156.jsonl`
- `benchmark/results/work/panda-confirm-20261001/traces/receive_dxgi_30_1_20261001T111250.jsonl`
