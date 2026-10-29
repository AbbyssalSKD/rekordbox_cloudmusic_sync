"""配置读取：各平台的登录 Cookie（全部可选）。

公开歌单三个平台都不需要 Cookie；私密歌单 / 「我喜欢的音乐」才需要。
Cookie 属于凭证，只放在环境变量或本地文件，绝不入库（见 .gitignore）。

优先级：命令行 > 环境变量（<SOURCE>_COOKIE）> config.local.json 的 <source>_cookie
"""

from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_LOCAL = Path("config.local.json")


def load_cookie(source: str, explicit: str | None = None) -> str:
    if explicit:
        return explicit.strip()
    env = os.environ.get(f"{source.upper()}_COOKIE", "").strip()
    if env:
        return env
    if CONFIG_LOCAL.is_file():
        try:
            data = json.loads(CONFIG_LOCAL.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return ""
        return str(data.get(f"{source}_cookie") or "").strip()
    return ""
