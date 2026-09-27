# 真实图片文件屏幕端到端基准

测试时间：2026-09-25 16:13（Asia/Singapore）  
取帧后端：DXGI Desktop Duplication  
浏览器：有头 Chromium，真实屏幕播放 `sender.html`；接收端：`desktop` 源

## 测试输入

- 文件：`C:\Users\GMKMIX\Pictures\Screenshots\屏幕截图 2026-08-17 193931.png`
- 大小：111,561 B（0.106 MiB）
- SHA-256：`567a65cf6c546dd8be79c319960813a58ad128a2b4f7ba3d2aa71391fe8ad863`
- 物理几何：BIT=10，PAD=4，COLS×ROWS=170×68，chunkSize=1,419 B
- 数据帧：79；采集区域：`left=0, top=89, width=1920, height=960`

## 结果

| 设定 FPS | 实测到达 FPS（中位） | 缺帧 | 接收/总帧 | 拒帧 | 墙钟耗时 | 吞吐 | SHA |
|---:|---:|---:|---:|---:|---:|---:|:---:|
| 15 | 14.31 | 0/79 | 79/79 | 1 | 5.823 s | 18.7 KiB/s | ✓ |
| 20 | 18.10 | 0/79 | 79/79 | 2 | 4.701 s | 23.2 KiB/s | ✓ |
| 30 | 28.02 | 0/79 | 79/79 | 2 | 2.960 s | 36.8 KiB/s | ✓ |

## 结论

这次真实屏幕链路在该机器、该窗口和该文件上 30 FPS 仍能完整还原，
实测吞吐约为 15 FPS 的 1.97 倍；校准三档均为缺帧 0，CRC 通过率 100%。
这是小文件且只有 79 个数据帧的结果，不能直接外推 8.4 MB 文件；大文件应
继续使用同一命令，把 `--reps` 提高到 3，并观察是否出现跨轮重播和缺帧尾部。

机器与原始 JSON/CSV 结果见：

- [live_desktop_results_dxgi_output.json](live_desktop_results_dxgi_output.json)
- [live_desktop_results_dxgi_output.csv](live_desktop_results_dxgi_output.csv)
