"""制带工具（视频信道场景，需求文档 §11）：文件 → 方块帧 MP4。

CRC / FEC 语义 import `receiver.protocol` / `receiver.fec`（单一事实
来源，防协议漂移；CRC 双端共用 receiver.protocol.frame_crc）；帧头字节
组装为本包写侧实现，与读侧 verify_crc 由交叉测试仲裁。渲染与编码布局
依据 ADR-0003（全 I 帧 + 轮次重复）。协议唯一基准：docs/protocol.md。
"""
