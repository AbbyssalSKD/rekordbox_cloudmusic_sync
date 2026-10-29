"""命令行包。入口在 ``main.py``，``rb-sync`` 与 ``python -m rb_sync`` 等价。"""

from .main import build_parser, main

__all__ = ["main", "build_parser"]
