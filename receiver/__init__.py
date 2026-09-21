"""接收端：跨隔离网络单向文件摆渡（spec 0001）。

入口 `python -m receiver`，子命令见 cli.py。协议唯一基准：docs/protocol.md。
"""

__all__ = ["cli", "pipeline", "protocol", "restore", "store"]
