# tapemaker 使用说明

> 术语见 `CONTEXT.md`：**制片**（文件 → 片的编码过程）、**片**（编码产物 MP4）、**轮次重复**。
> 历史注记：这对术语原名「制带 / 带」，2026-09-29 更名；CLI 参数 `--tape` 与模块名 `tapemaker` 沿用不改。

## 这是什么

`tapemaker` 是**视频信道**场景的制片工具：把任意文件编码成一段黑白方块视频（MP4），人工上传到视频平台（如 B 站），对端人工下载后用接收端还原。上传与下载均为人工操作，本工具不承担。

原理一句话：文件 → gzip 压缩 → 切成分片 → 每个分片画成一帧黑白方块（黑=1，白=0）→ 全 I 帧编码 + 整序列轮次重复 → MP4。协议与 sender.html 完全同源（CRC / FEC 语义直接 import `receiver.protocol` / `receiver.fec`），编码策略见 [ADR-0003](adr/0003-制片全I帧编码与轮次重复.md)。

## 快速上手

### GUI（推荐，双击可用）

两种形态任选：

- **独立 exe**：`dist/tapemaker_gui.exe`（PyInstaller onefile 打包，双击即用，无需 Python 环境；构建命令见 `tapemaker_gui.spec` 头注）；
- **脚本版**：双击仓库根目录的 **`tapemaker_gui.pyw`**（pythonw 启动，无黑窗口；首次双击会自动转手仓库 venv 解释器）。

界面与流程：

1. 「浏览…」选待摆渡文件——输出 MP4 自动回填为同目录同名 `.mp4`；
2. 参数默认即可（BIT 留空 = 按分辨率默认档；窗口内有权衡提示）；
3. 点「开始制片」，完成后显示出片摘要与下一步接收命令。

命令助手实时显示等价 CLI 命令，可复制给需要命令行的场合。calibrate 定标不进 GUI，仍走命令行。

### 命令行

```powershell
# 1. 制片（全默认即可）
.venv/Scripts/python.exe -m tapemaker make 文件 -o out.mp4

# 2.（强烈建议）上传前自检：本机解码还原，确认片本身没问题
.venv/Scripts/python.exe -m receiver receive --source video --video out.mp4 --tape --out out_pre
#    比对还原文件与原文件一致（receiver 输出末尾有 sha256）

# 3. 人工上传 out.mp4 到视频平台（不要加片头/水印等平台侧编辑）

# 4. 对端人工下载最高清晰度版本，还原：
.venv/Scripts/python.exe -m receiver receive --source video --video 下载的视频.mp4 --tape --out out_post
```

`make` 跑完会打印：压缩后字节数、fileId、几何参数、帧数与时长；`--tape` 是 video 源的片模式（issue #45），旁路稳定闸门逐帧直读制片 MP4，务必带上。

## make 参数

```text
python -m tapemaker make <文件> -o out.mp4 [--fps N] [--bit N] [--pad N] [--rounds N] [--resolution 1080p|4k]
```

| 参数 | 默认 | 含义 |
|---|---|---|
| `--resolution` | 1080p | 画布尺寸档。4K 画布更密、单帧容量更大，但小方块更怕二压 |
| `--bit` | 1080p=8 / 4k=15 | 单个方块边长（物理像素，1–15）。**核心权衡**：方块越大越抗压缩，但一帧装的数据越少 → 帧数越多 → 片越长 |
| `--pad` | 3 | 方块间静默区宽（×BIT 像素，3–15）。防止二压模糊导致相邻方块粘连误读 |
| `--fps` | 30 | 播放帧率。帧总数不变，只影响片长与体积；过快可能加重二压损失 |
| `--rounds` | 2 | 轮次重复：完整帧序列连播 N 轮（N ≥ 2，ADR-0003）。抗二压的核心冗余——接收端按帧号幂等落盘，任意一轮通过 CRC 即收下 |

## calibrate 参数（模拟二压定标）

```text
python -m tapemaker calibrate <样本文件> -o <目录> [--crf 18,23,28] [--bit 档位] [--resolution 档位] [--strategy 档位] [--fps N] [--rounds N]
```

定标**不产出可用的片**，而是实验台：对同一内容按各参数组合制片 → ffmpeg 按 CRF 档重编码模拟平台二压 → 逐帧过 receiver 流水线，统计 CRC 存活率，输出存活率表 + 推荐默认参数。

| 参数 | 默认 | 含义 |
|---|---|---|
| `--crf` | 18,23,28 | 模拟二压强度档（值越大压得越狠）。平台实际参数不可知，档位即二压强度假设 |
| `--bit` | 按分辨率取 [默认值, 默认值−2] | 待试的方块大小档位（逗号分隔） |
| `--resolution` | 1080p | 待试的画布档位（逗号分隔，1080p/4k） |
| `--strategy` | allintra | I 帧策略档位：`allintra` 每帧独立编码（最抗二压，体积大）；`gop` 每重复单元一 I 帧（体积小，误差可在副本间累积） |
| `--fps / --rounds / --pad` | 同 make | 定标的固定条件 |

依赖 **ffmpeg 在 PATH 中**（缺失时报错并提示 `winget install Gyan.FFmpeg`）。

推荐口径：在最严 CRF 档下 100% 存活的组合中，选最小 BIT（片最短），并列时选样片更小的策略。输出会直接给出 `make` 可用的回填参数。

## 何时调参

只有两种情形需要碰参数，其余用默认：

1. **还原失败**（平台压得太狠，CRC 拒帧多）→ 加大 `--rounds`，或调大 `--bit`。
2. **片太长 / 太大** → 调小 `--bit` 或降 `--rounds`，但先用 `calibrate` 确认新档位在最严假设下仍 100% 存活。

默认值的效力目前来自 calibrate 的**模拟**二压；真实平台的二压强度需实测定标（issue #41），定标结论回填本工具默认值。

## 完整链路与职责划分

| 环节 | 执行者 |
|---|---|
| 制片 | `tapemaker make` |
| 上传视频平台 / 下载 | **人工** |
| 还原 | `receiver receive --source video --video <片> --tape` |
| 定标 | `tapemaker calibrate` + 真实平台投稿实测（issue #41） |
