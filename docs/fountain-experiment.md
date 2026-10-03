# 喷泉码实验版本

分支：`codex/fountain-experiment`。默认仍使用原有固定 FEC。
实验模式是每组 32 分片的系统式 GF(2) 随机线性喷泉码，不是 LT/RaptorQ。
用于验证丢帧后持续发送新修复帧，能否缩短完整文件重播带来的等待。

## 使用

1. 使用本分支的 `sender.html`，在“纠错模式”选择“喷泉码（实验 v2）”。
2. 使用本分支接收端；GUI 和 CLI 自动识别，无需额外参数。
3. 首轮发送原始分片及每组两个修复帧；随后只发送新修复帧。
4. 等接收端确认还原后停止。修复批次不代表完整文件重播轮次。

同一文件切换纠错模式时，应清空其旧的未完成任务进度。已恢复分片可断点续传，
尚未解出的修复方程只存内存，重启后重新累计。原有视频制作工具仍输出 v1。

导出 PNG 会附每组共 18 个修复帧，是有限序列，不保证任意损失或中途开始都能恢复；
在线播放则持续生成新修复帧。修复编号耗尽后重新发送原始数据轮次。

## 对比方法

固定种子仿真：

```powershell
.venv/Scripts/python.exe benchmark/run_bench_fountain.py --seeds 20
```

真实桌面测试（两次必须使用同一文件、FPS、窗口和采集后端；依次运行）：

```powershell
.venv/Scripts/python.exe benchmark/run_bench_live.py --input sample.bin --capture dxgi --coding fec --fps-list 30 --reps 2 --skip-calibration --trace --run-label fec-test
.venv/Scripts/python.exe benchmark/run_bench_live.py --input sample.bin --capture dxgi --coding fountain --fps-list 30 --reps 2 --skip-calibration --trace --run-label fountain-test
```

此实验不使用 calibrate 的重复轮次指标评价喷泉码；新修复帧并不重复原始帧。
结果应同时看 SHA-256、端到端 wall 时间、99% 后等待、拒帧和队列丢弃。
仿真的帧数/FPS 只代表信道占用，未包含抓屏、网格识别、磁盘和浏览器调度。

协议细节见 [protocol.md](protocol.md) 文末 v2 扩展，实验结果见
[基准报告](../benchmark/results/report-fountain-experiment-20261003.md)。
