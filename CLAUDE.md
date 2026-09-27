# CLAUDE.md

## 项目概览

跨隔离网络单向文件摆渡系统：文件经「屏幕显示 → 拍摄/抓屏采集 → 识别还原」这一唯一单向信道从内网传到外网。

- **发送端**：`sender.html` —— 浏览器单文件页面（原生 JS，无构建步骤），把文件编码为黑白方块帧序列，支持导出 PNG 序列（`frames_png/` + `frames.json`）、全屏/窗口播放。
- **接收端**：`receiver/` —— Python 包（≥3.11），入口 `python -m receiver`（契约见需求文档 §6）。取帧源 4 种：camera / video / images / desktop（Windows 自动优先 DXGI，不可用时回退 mss），位于 `receiver/sources/`。
- **测试**：`tests/`，含端到端闭环测试（playwright 驱动 sender.html 导出 PNG → images 源解码逐字节比对）。

## 关键命令

```powershell
# 测试 —— 必须用仓库根 .venv/ 的解释器（代码里写的 venv/ 路径不存在）
.venv/Scripts/python.exe -m pytest

# e2e 闭环测试需要浏览器二进制，未安装时自动跳过
.venv/Scripts/python.exe -m playwright install chromium

# 接收端 CLI（receive / calibrate 两个子命令；取帧源 4 种）
.venv/Scripts/python.exe -m receiver receive --source images --dir frames_png
.venv/Scripts/python.exe -m receiver receive --source desktop --region 17,197,1862,775   # --region pick 为冻屏框选
```

注意：`pytest.ini` 的 `--basetemp=.tmp` 会**每次运行清空 `.tmp/`**，勿在其中存放任何文件。

## 冻结项与文档约定

- **协议是冻结项**：`docs/protocol.md` 是双端实现的唯一协议对齐基准，事实来源为需求文档 §3。黑块=1/白块=0、CRC 覆盖范围（偏移 2–21 + 数据区有效字节）、元数据帧布局等均不得擅改；改协议须先改需求文档与 protocol.md。
- **术语表**：`CONTEXT.md`（根目录，single-context 布局）。写 issue、重构提案、测试命名时使用其中定义的术语（摆渡、帧头、PAD、几何自举、参数锁定等），不要用同义词漂移。
- **ADR**：`docs/adr/`（如 0001 角标编码几何自举）。与现有 ADR 冲突时显式指出，不要静默绕过。
- 详细消费规则见 `docs/agents/domain.md`。

## Agent skills

### Issue tracker

Issues 存放在本仓库的 GitHub Issues 中，使用 `gh` CLI 操作。见 `docs/agents/issue-tracker.md`。

### Triage labels

使用五个默认 triage 标签（`needs-triage` / `needs-info` / `ready-for-agent` / `ready-for-human` / `wontfix`，标签名与语义一致）。见 `docs/agents/triage-labels.md`。

### Domain docs

single-context 布局：根目录 `CONTEXT.md` + `docs/adr/`。见 `docs/agents/domain.md`。

## 约定

- **提交信息**：`sender: …（issue #N）` / `receiver: …（issue #N）` / `fix: …（issue #N）`，中文描述，关联 issue 编号。
- **平台**：Windows 11，主 shell 为 PowerShell；路径处理注意 MAX_PATH（pytest 已通过 basetemp 锚定规避）。
- **依赖**：接收端依赖见 `requirements.txt`（opencv-python、numpy、rich、mss、winotify、playwright）；发送端零依赖（纯浏览器）。
