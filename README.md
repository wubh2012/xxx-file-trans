简体中文 · [English](README_EN.md)

<div align="center">

# xxx-file-trans

把文件变成黑白方块图，再从屏幕、视频或 PNG 序列中还原。

[![Windows](https://img.shields.io/badge/Windows-11-0078D4)](#快速开始)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB)](#从源码运行)
[![License: MIT](https://img.shields.io/badge/License-MIT-green)](LICENSE)

[下载](https://github.com/wubh2012/xxx-file-trans/releases/latest) · [主要功能](#主要功能) · [快速开始](#快速开始) · [工作流程](#工作流程) · [常见问题](#常见问题) · [文档与贡献](#文档与贡献)

</div>

这个工具用画面传文件。发送端把文件压缩后拆成分片，画成连续的黑白方块图；接收端读取这些画面，还原出原文件。两端不用联网，接收端也不用回复确认。

可以直接在浏览器里播放，让接收端抓屏；也可以先做成 MP4 或导出 PNG 序列，再把视频或帧目录交给对方。

## 适用场景

如果文件没法直接拷过去，但对方能看到播放画面，或者能拿到一段视频，就可以试试这个办法。日志、配置、报表和小型资料包都可以传。

下面的步骤按 Windows 11 编写。`camera` 目前只是接口骨架，还不能用摄像头实时接收。通过远程桌面或视频平台传输时，先用小文件试一下，缩放和压缩都可能影响识别。

## 演示

- [测试视频 1（BV1Euan6zEL8）](https://www.bilibili.com/video/BV1Euan6zEL8/)
- [测试视频 2（BV1ata16YEWg）](https://www.bilibili.com/video/BV1ata16YEWg/)

可以用这两个视频试接收端。播放时选最高画质，把完整的黑白方块图留在屏幕上。平台压缩、播放器缩放和抓屏环境会影响还原结果。

## 主要功能

- 发送端直接在浏览器打开，支持窗口播放、全屏播放和 PNG 序列导出，不用安装依赖。
- 接收端有 GUI 和 Python 命令行，可以抓屏，也可以读取视频或 PNG 帧目录。
- 制片工具能直接把文件做成 MP4，省去录屏。对方拿到视频后可以离线还原。
- 每帧做 CRC32 校验，损坏帧会被丢弃。前向纠错和重播用于补齐缺帧；已收进度会保存，退出后还能继续收。
- v1.1.0 加入了实验性的喷泉码模式：首轮穿插 XOR 修复帧，之后不断生成新的修复帧。可选比例为 32:1、32:2、16:1、8:1，接收端自动识别。用法与兼容要求见 [实验说明](docs/fountain-experiment.md)。
- 还原时检查 gzip 完整性，完成后显示文件名、大小、耗时和 SHA-256 摘要。

喷泉码主要解决“文件快收齐了，最后几帧还要等下一轮”的问题：发送端持续提供
新的修复信息，接收端用它算出丢失内容，无需反向请求。本机 MSS 抓屏实测
中位耗时缩短 36.1%，但稳定 DXGI 抓屏慢了 5.9%，因此仍保留固定 FEC 为默认。
通俗原理、引入原因和完整对照见 [喷泉码说明](docs/fountain-experiment.md)。

## 快速开始

### 下载 Windows 版本

无需配置 Python，前往 [最新 Release](https://github.com/wubh2012/xxx-file-trans/releases/latest) 下载：

| 文件 | 用途 |
|---|---|
| [tapemaker_gui.exe](https://github.com/wubh2012/xxx-file-trans/releases/latest/download/tapemaker_gui.exe) | 制作端：选择文件，编码生成 MP4 视频 |
| [receiver_gui.exe](https://github.com/wubh2012/xxx-file-trans/releases/latest/download/receiver_gui.exe) | 接收端：通过桌面抓屏、视频文件或 PNG 序列还原文件 |
| [sender.html](https://github.com/wubh2012/xxx-file-trans/releases/latest/download/sender.html) | 浏览器发送端：下载后用现代浏览器打开，播放画面或导出 PNG 序列 |
| [SHA256SUMS.txt](https://github.com/wubh2012/xxx-file-trans/releases/latest/download/SHA256SUMS.txt) | 下载文件的 SHA-256 校验值 |

将 EXE 放到可写目录后双击运行。接收端默认在 EXE 所在目录创建 `output/`、`progress/` 和 `debug/`。**制作端生成 MP4 仍需要安装 FFmpeg，并将其加入 PATH**；安装后重新打开制作端。源码运行见下文。

### 完成第一次传输（GUI）

第一次用，先在同一台电脑上发一个小文件，确认能还原。

1. 用现代浏览器打开 `sender.html`，点击「预设：desktop 抓屏」，使用窗口播放、BIT 6、FPS 20。
2. 选择或拖入一个小文件，点击「开始播放」，保持完整画面可见。
3. 打开 `receiver_gui.exe`，选择「抓屏（desktop）」→「框选…」，在冻屏快照上圈住完整数据区、四个角标及周围静默边。
4. 点击「开始接收」，等待「还原完成」摘要。还原文件默认保存在 EXE 所在目录的 `output/`。
5. 确认成功后，在发送页面按 Esc 或点击「停止播放」。发送端无法获知接收进度，需人工停止。

**先播放，再框选。** 避免窗口遮挡、画面裁切、切到后台标签页或屏幕熄灭。按 Esc 取消框选会中止接收。

### 从源码运行

接收端需要 Python ≥ 3.11。发送页面 [sender.html](sender.html) 用现代浏览器打开即可，不用构建。下面的命令在 PowerShell 中运行：

```powershell
git clone https://github.com/wubh2012/xxx-file-trans.git
cd xxx-file-trans
py -3.11 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
```

如果使用其他 ≥ 3.11 的 Python 版本，将 `py -3.11` 换成对应版本。

在浏览器打开 `sender.html`，按上面的 GUI 步骤开始播放，再运行命令行接收端：

```powershell
.venv/Scripts/python.exe -m receiver receive --source desktop --region pick
```

命令会打开冻屏框选界面，圈住播放画面即可。也可以启动 GUI：

```powershell
.venv/Scripts/pythonw.exe receiver_gui.pyw
```

Windows 下默认 `--capture auto` 优先使用 DXGI，不可用时回退 MSS；命令行启动时显示实际采集后端。先用预设确认稳定性，再尝试提高到 30 FPS。

接收完成后会显示文件名、大小、耗时、帧数和 SHA-256 摘要。源码运行时，文件默认写到仓库根目录的 `output/`。

如果两份文件都能访问，可以核对哈希。以 `sample.txt` 为例：

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath sample.txt
Get-FileHash -Algorithm SHA256 -LiteralPath output/sample.txt
```

两份哈希应一致。确认后手动停止发送页面。

## 工作流程

```text
发送端                      画面通道                     接收端

文件 → 压缩、分片 → 浏览器窗口循环播放 ── 桌面抓屏 ──→ 还原文件
                  → 制成 MP4 ──────── 人工交接 ──→ 离线还原
                  → 导出 PNG 序列 ─── 人工交接 ──→ 离线还原
```

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

重新播放同一文件，再启动相同的接收流程即可继续补齐缺失分片。已收进度保存在 `progress/`；同一任务的几何参数、分片大小和纠错模式必须保持一致。切换纠错模式前应备份需要保留的进度，再清空对应未完成任务；喷泉码未解出的修复方程只存内存，重启后需重新累计。

默认固定 FEC 模式通过循环重播补齐；每组最多 32 个数据帧附带 2 个校验帧，条件满足时可恢复同组最多 2 个缺失数据帧。详细行为见[传输协议](docs/protocol.md)。

## 文件大小与实测性能

先用 1～4 MB 的文件试一下。本机测试中，1～5 MB 的文件适合日常摆渡，10 MB 难压缩内容需要预留约 3 分钟。文本压缩率高的话，会快不少。

| 样本 | 原始大小 | 实测耗时 |
|---|---:|---:|
| 难压缩二进制 | 1 MB | 15.3 秒 |
| 难压缩二进制 | 5 MB | 80.5 秒 |
| 难压缩二进制 | 10 MB | 160.8 秒 |
| 模拟应用日志 | 1 MB | 3.0 秒 |
| 模拟应用日志 | 10 MB | 28.4 秒 |
| 模拟业务 CSV | 5 MB | 31.3 秒 |

以上为 2026-10-01 的真实桌面闭环测试：DXGI 采集、30 FPS、清晰无遮挡的浏览器窗口，每数据帧承载 2,431 字节，每个样本正式测试一次。计时覆盖文件载入、压缩、播放、接收、还原和写盘；六个样本均通过 SHA-256 与逐字节比对，无需重播补齐。1 MB = 1,000,000 字节。

二进制为伪随机样本，日志和 CSV 为合成数据。这里只测了这几个样本，速度和可传的文件大小还要看实际环境。更大文件、摄像头、远程桌面和视频二次压缩都没包含在本轮测试中。完整条件见[文件大小实测报告](benchmark/results/report-size-matrix-20261001.md)，另有[3.57 MB JPG 的三次实测](benchmark/results/report-panda-optimized-20261001.md)。

## 本地数据与使用边界

源码运行默认在仓库根目录保存数据；EXE 默认在程序所在目录保存：

| 目录 | 用途 |
| --- | --- |
| `output/` | 还原文件，可用 `--out` 指定其他位置 |
| `progress/` | 接收进度；删除对应任务会丢失其续传数据 |
| `debug/` | 调试画面目录 |

还有几个使用条件：

| 项目 | 当前边界 |
|---|---|
| 运行环境 | 源码步骤以 Windows 11 和 PowerShell 为基准，需要 Python ≥ 3.11；Release 中的 EXE 无需 Python |
| 摄像头拍屏 | `camera` 源仅为接口骨架，完整实时采集尚未实现 |
| 画面质量 | 播放区域需保持清晰、完整、无遮挡；远程桌面缩放与压缩需要在目标环境验证 |
| 视频平台 | 上传、下载由人工完成；平台二次压缩后的还原效果需要实测定标 |
| 安全能力 | 未内置文件加密、身份认证或数字签名；CRC 与纠错用于处理损坏和缺帧 |

## 常见问题

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

发到视频平台后仍要试着还原一次。定标工具的说明见[制片工具文档](docs/tapemaker.md)。

### 续传出现参数不一致告警

恢复原来的发送参数。若要用新参数重新传输，应先备份仍需保留的进度，再手动清空 `progress/` 中对应任务目录，从头接收。

### EXE 制作端提示找不到 FFmpeg

安装 FFmpeg，将包含 `ffmpeg.exe` 的目录加入 PATH，然后重新打开制作端。EXE 自带 Python 运行环境，FFmpeg 需要另装。

## 技术栈与项目结构

| 部分 | 技术 |
| --- | --- |
| 浏览器发送端 | 单文件 HTML、原生 JavaScript、Canvas |
| 接收与还原 | Python ≥ 3.11、OpenCV、NumPy |
| 桌面采集 | DXGI（dxcam）、MSS |
| 图形界面 | Tkinter；PyInstaller 打包 Windows EXE |
| 视频制作 | Python、FFmpeg |
| 测试 | pytest、Playwright 浏览器闭环测试 |

```text
.
├── sender.html          # 浏览器发送端
├── receiver/            # 接收、解码、纠错与还原
├── receiver_gui.pyw     # 接收端 GUI
├── tapemaker/           # MP4 制片与二压定标
├── tapemaker_gui.pyw    # 制作端 GUI
├── tests/               # 单元与端到端测试
├── benchmark/           # 基准工具、报告与结果
├── docs/                # 协议、使用说明与架构决策
└── build_exe.ps1        # Windows 打包脚本
```

## 文档与贡献

以下详细文档目前以中文为主：

- [制片工具](docs/tapemaker.md)：MP4 生成、还原与二压定标。
- [文件大小实测报告](benchmark/results/report-size-matrix-20261001.md)：样本、测试条件与原始结果。
- [基准工具](benchmark/README.md)：性能测试与诊断。
- [传输协议](docs/protocol.md)：帧格式、校验、纠错与续传约定。
- [需求文档](需求文档.md)：需求与场景说明。
- [领域术语](CONTEXT.md)与[架构决策](docs/adr/)：项目概念、命名和设计依据。
- [喷泉码实验](docs/fountain-experiment.md)：原理、兼容要求、使用方式和对照结果。

通过仓库 [Issues](https://github.com/wubh2012/xxx-file-trans/issues) 反馈问题或提出改进建议。接收问题请附操作系统、Python 版本、发送参数、取帧源、实际采集后端、报错和复现步骤；视频问题还应说明是否经过二次压缩。

修改前请阅读[传输协议](docs/protocol.md)及[领域术语](CONTEXT.md)。协议是双端对齐基准，涉及协议变更时需同步更新需求、协议文档和双方实现；面向用户的说明请同步维护中英文 README。

### 运行测试

在仓库根目录使用 `.venv` 的解释器：

```powershell
# 端到端测试需要 Chromium，首次运行前安装
.venv/Scripts/python.exe -m playwright install chromium
.venv/Scripts/python.exe -m pytest
```

注意：测试配置会清空 `.tmp/`，不要在其中存放需要保留的文件，测试后及时清理临时产物。

### 打包 Windows 制作端与接收端

```powershell
.\build_exe.ps1
```

产物为 `dist/receiver_gui.exe` 和 `dist/tapemaker_gui.exe`。两个独立程序均无需 Python 环境；制作端生成 MP4 仍依赖 PATH 中的 FFmpeg。接收端默认的 `output/`、`progress/` 和 `debug/` 位于 EXE 所在目录。

## 许可证

本项目采用 [MIT 许可证](LICENSE)。
