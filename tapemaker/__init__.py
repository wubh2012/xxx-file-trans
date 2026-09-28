"""制带工具（视频信道场景，需求文档 §11）：文件 → 方块帧 MP4。

帧头 / CRC / FEC 实现 import `receiver.protocol` / `receiver.fec`
（单一事实来源，防协议漂移）；渲染与编码布局依据 ADR-0003
（全 I 帧 + 轮次重复）。协议唯一基准：docs/protocol.md。
"""
