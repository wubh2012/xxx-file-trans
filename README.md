# xxx-file-trans

**通过屏幕画面、视频或 PNG 序列，在没有直接网络连接的两端之间传递文件。**

面向普通文件传输通道不可用、但仍能显示或交接画面的场合：发送端把文件编码为连续的黑白方块画面，接收端读取画面并还原文件。适合日志、配置、报表和小型资料包的单向摆渡，无需接收端逐帧确认。

## 使用效果

```text
发送端                      画面通道                     接收端

文件 → 压缩、分片 → 浏览器窗口循环播放 ── 桌面抓屏 ──→ 还原文件
                  → 制成 MP4 ──────── 人工交接 ──→ 离线还原
                  → 导出 PNG 序列 ─── 人工交接 ──→ 离线还原
```

例如：发送方在浏览器中载入一份日志，接收方框选可见的播放画面；收齐分片后，接收端将日志写入输出目录，并显示「还原完成」摘要。

## 主要能力与限制

- **浏览器发送**：直接打开单文件页面，无需安装发送端依赖；支持窗口播放、全屏播放和 PNG 序列导出。
- **多种接收方式**：通过桌面抓屏、视频文件或 PNG 序列还原；提供 Python 命令行和 GUI。
- **视频异步交接**：制片工具把文件直接编码为 MP4，接收方取得视频后离线还原，无需先录屏。
- **丢帧补齐与续传**：逐帧 CRC32 校验、前向纠错、循环重播和持久化接收进度共同处理损坏、缺帧与中断。
- **完整性核对**：还原时检查 gzip 完整性，完成后显示文件名、大小、耗时和 SHA-256 摘要。

当前最直接的两条路径是 **浏览器窗口播放 + 桌面接收**、**MP4 制片 + 离线还原**。使用前请留意：

| 项目 | 当前边界 |
|---|---|
| 运行环境 | 以下步骤以 Windows 11 和 PowerShell 为基准；接收端与制片工具需要 Python ≥ 3.11 |
| 摄像头拍屏 | `camera` 源仅为接口骨架，完整实时采集尚未实现 |
| 画面质量 | 播放区域需保持清晰、完整、无遮挡；远程桌面缩放与压缩需要在目标环境验证 |
| 视频平台 | 上传、下载由人工完成；平台二次压缩后的还原效果需要实测定标 |
| 安全能力 | 未内置文件加密、身份认证或数字签名；CRC 与纠错用于处理损坏和缺帧 |

### 文件大小与实测耗时

建议先用 **1～4 MB 的代表性文件**验证环境。当前实测表明，1～5 MB 适合日常摆渡，10 MB 难压缩内容应预留约 3 分钟；可压缩文本可能明显更快。

| 样本 | 原始大小 | 实测耗时 |
|---|---:|---:|
| 难压缩二进制 | 1 MB | 15.3 秒 |
| 难压缩二进制 | 5 MB | 80.5 秒 |
| 难压缩二进制 | 10 MB | 160.8 秒 |
| 模拟应用日志 | 1 MB | 3.0 秒 |
| 模拟应用日志 | 10 MB | 28.4 秒 |
| 模拟业务 CSV | 5 MB | 31.3 秒 |

以上为 2026-10-01 的真实桌面闭环测试：DXGI 采集、30 FPS、清晰无遮挡的浏览器窗口，每数据帧承载 2,431 字节，每个样本正式测试一次。计时覆盖文件载入、压缩、播放、接收、还原和写盘；六个样本均通过 SHA-256 与逐字节比对，无需重播补齐。1 MB = 1,000,000 字节。

二进制为伪随机样本，日志和 CSV 为合成数据。这些结果不是速度保证或文件大小上限；更大文件、摄像头、远程桌面和视频二次压缩未包含在本轮测试中。完整条件见[文件大小实测报告](benchmark/results/report-size-matrix-20261001.md)，另有[3.57 MB JPG 的三次实测](benchmark/results/report-panda-optimized-20261001.md)。

## 快速开始：窗口播放与桌面接收

先在同一台机器完成一次小文件闭环，确认发送、抓屏和还原正常，再用于实际画面通道。

### 1. 准备接收环境

发送端使用现代浏览器打开 [sender.html](sender.html)。接收端安装 Python ≥ 3.11 后，在仓库根目录运行以下 PowerShell 命令：

```powershell
py -3.11 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
```

如果使用其他 ≥ 3.11 的 Python 版本，将 `py -3.11` 换成对应版本。

### 2. 先开始播放

1. 浏览器打开 `sender.html`，点击「预设：desktop 抓屏」，使用窗口播放、BIT 6、FPS 20。
2. 点击选择或拖入一个小文件，例如 `sample.txt`。
3. 点击「开始播放」，让完整黑白方块画面出现在屏幕上。

保持播放窗口可见，避免其他窗口遮挡、切换到后台标签页或让屏幕熄灭。

### 3. 再框选并接收

在接收侧运行：

```powershell
.venv/Scripts/python.exe -m receiver receive --source desktop --region pick
```

在冻屏快照上拖出矩形，包含完整数据区、四个角标及周围静默边。**必须先播放再框选**，否则快照中没有待采集的帧画面。按 Esc 取消框选会中止接收。

也可启动 GUI：

```powershell
.venv/Scripts/pythonw.exe receiver_gui.pyw
```

选择「抓屏（desktop）」→「框选…」→圈住播放画面→「开始接收」。

Windows 下默认 `--capture auto` 优先使用 DXGI，不可用时回退 MSS；命令行启动时显示实际采集后端。先用预设确认稳定性，再尝试提高到 30 FPS。

### 4. 确认成功并停止播放

成功时，接收端显示「还原完成」以及文件名、大小、耗时、帧数和 SHA-256 摘要。脚本版默认把文件写入仓库根目录的 `output/`。

例如传输的是 `sample.txt`，可以在能访问原文件与还原文件的环境中核对完整哈希：

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath sample.txt
Get-FileHash -Algorithm SHA256 -LiteralPath output/sample.txt
```

两份哈希应一致。确认接收完成后，在发送页面按 Esc 或点击「停止播放」。发送端无法获知接收进度，会继续循环播放，需人工停止。

## 常用任务

### 将文件制成 MP4，再离线还原

制片另需 **ffmpeg 在 PATH 中**。下面以仓库根目录已有的 `sample.zip` 为例：

```powershell
# 制片
.venv/Scripts/python.exe -m tapemaker make "sample.zip" -o out.mp4

# 交接前先在本机还原，核对文件与原件一致
.venv/Scripts/python.exe -m receiver receive --source video --video out.mp4 --out out_pre

# 接收方取得视频后还原
.venv/Scripts/python.exe -m receiver receive --source video --video out.mp4
```

视频文件默认逐帧解析，制片 MP4 和普通录屏均无需 `--tape`（保留该参数兼容旧命令）。上传、下载视频由人工完成；平台可能改变分辨率、码率或画面，建议用代表性样本验证实际交接链路。

GUI 可用 `.venv/Scripts/pythonw.exe tapemaker_gui.pyw` 启动。参数、轮次重复与二压定标见[制片工具文档](docs/tapemaker.md)。

### 从 PNG 序列或普通录屏还原

在发送页面点击「导出 PNG 帧序列」，将导出的帧目录交给接收端，或提供对播放画面的普通录屏：

```powershell
# PNG 序列
.venv/Scripts/python.exe -m receiver receive --source images --dir frames_png

# 普通录屏，不加 --tape
.venv/Scripts/python.exe -m receiver receive --source video --video recording.mp4
```

需要指定输出位置时，在接收命令后增加 `--out <目录>`。

### 中断后继续接收

重新播放同一文件，再启动相同的接收流程即可继续补齐缺失分片。已收进度保存在 `progress/`；同一任务的几何参数和分片大小必须保持一致。

实时播放通过循环重播补齐；每组最多 32 个数据帧附带 2 个校验帧，条件满足时可恢复同组最多 2 个缺失数据帧。详细行为见[传输协议](docs/protocol.md)。

## 常见问题与文档

### 框选后没有进度，或一直收不齐

检查框选区域是否完整包含画布和四个角标，播放窗口是否被遮挡、裁切或切到后台。先降低播放 FPS，再观察接收是否稳定；提高播放帧率可能增加缺帧和重播等待。

如需统计识别情况，可运行以下命令，采样后按 Ctrl+C 输出报告：

```powershell
.venv/Scripts/python.exe -m receiver calibrate --source desktop --region pick
```

### 视频还原失败

确认视频文件存在且可读取：制片 MP4 与普通录屏均默认逐帧解析，无需选择模式。先还原原始制片视频，再比较交接后的版本，以判断是否受到二次压缩影响。模拟二压定标命令：

```powershell
.venv/Scripts/python.exe -m tapemaker calibrate "sample.zip" -o calibration
```

模拟结果不能替代实际平台验证，详细说明见[制片工具文档](docs/tapemaker.md)。

### 续传出现参数不一致告警

恢复原来的发送参数。若要用新参数重新传输，应先备份仍需保留的进度，再手动清空 `progress/` 中对应任务目录，从头接收。

### 进一步阅读

- [制片工具](docs/tapemaker.md)：MP4 生成、还原与二压定标。
- [文件大小实测报告](benchmark/results/report-size-matrix-20261001.md)：样本、测试条件与原始结果。
- [基准工具](benchmark/README.md)：性能测试与诊断。
- [传输协议](docs/protocol.md)：帧格式、校验、纠错与续传约定。
- [需求文档](需求文档.md)：需求与场景说明。
- [领域术语](CONTEXT.md)与[架构决策](docs/adr/)：项目概念、命名和设计依据。

## 问题反馈与参与贡献

通过仓库 [Issues](https://github.com/wubh2012/xxx-file-trans/issues) 反馈问题或提出改进建议。接收问题请附操作系统、Python 版本、发送参数、取帧源、实际采集后端、报错和复现步骤；视频问题还应说明是否经过二次压缩。

修改前请阅读[传输协议](docs/protocol.md)及[领域术语](CONTEXT.md)。协议是双端对齐基准，涉及协议变更时需同步更新需求、协议文档和双方实现。

### 运行测试

在仓库根目录使用 `.venv` 的解释器：

```powershell
# 端到端测试需要 Chromium，首次运行前安装
.venv/Scripts/python.exe -m playwright install chromium
.venv/Scripts/python.exe -m pytest
```

注意：测试配置会清空 `.tmp/`，不要在其中存放需要保留的文件。

### 打包 Windows 接收程序

```powershell
.\build_exe.ps1
```

产物为 `dist/receiver_gui.exe`。独立程序无需 Python 环境，其默认 `output/`、`progress/` 和 `debug/` 位于 exe 所在目录。

## 许可证

本项目采用 [MIT 许可证](LICENSE)。
