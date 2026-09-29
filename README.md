# xxx-file-trans

跨隔离网络单向文件摆渡系统：文件经「屏幕显示 → 拍摄/抓屏采集 → 识别还原」这一唯一单向信道传输。协议唯一基准：`docs/protocol.md`；术语表：`CONTEXT.md`；需求与场景全貌：`需求文档.md`。

## 两个场景

- **跨隔离网络摆渡**：发送端 `sender.html`（浏览器单文件，零依赖）把文件编码为黑白方块帧序列，全屏/窗口播放或导出 PNG 序列；接收端 `receiver/`（Python ≥ 3.11）经 camera / video / images / desktop 四种取帧源识别还原。

  ```powershell
  # 发送端：浏览器打开 sender.html，载入文件即开始播放
  # 接收端（按发送模式三选一）
  .venv/Scripts/python.exe -m receiver receive --source images --dir frames_png
  .venv/Scripts/python.exe -m receiver receive --source desktop --region 17,197,1862,775
  .venv/Scripts/python.exe -m receiver receive --source video --video tape.mp4
  ```

- **视频信道（制片）**：无屏幕、无摄像头——`tapemaker/` 把文件制成方块帧 MP4（片），人工上传视频平台、下载后用接收端 `--source video --tape` 识别还原。GUI：双击 `tapemaker_gui.pyw`。编码策略见 `docs/adr/0003`（全 I 帧 + 轮次重复）；使用说明见 `docs/tapemaker.md`。

  ```powershell
  .venv/Scripts/python.exe -m tapemaker make <文件> -o out.mp4 [--fps N] [--bit N] [--pad N] [--rounds N] [--resolution 1080p|4k]
  .venv/Scripts/python.exe -m tapemaker calibrate <样本文件> -o <目录>   # 模拟二压矩阵定标：CRC 存活率表 + 推荐参数
  ```

## 测试

```powershell
.venv/Scripts/python.exe -m pytest          # 必须用仓库根 .venv/ 的解释器
.venv/Scripts/python.exe -m playwright install chromium   # e2e 闭环测试需要浏览器二进制
```

## 文档

- `需求文档.md` —— 需求与协议事实来源
- `docs/protocol.md` —— 传输协议（冻结项）
- `docs/adr/` —— 架构决策记录
- `CONTEXT.md` —— 领域术语表
