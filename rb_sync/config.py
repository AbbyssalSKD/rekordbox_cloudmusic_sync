"""配置：库路径、曲库目录、各平台 Cookie、Web 监听地址。

优先级：命令行 > 环境变量 > config.local.json > 默认值。
配置文件里可能有 Cookie，**永远不要提交**（`.gitignore` 已排除 config.local.json）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_LOCAL = Path("config.local.json")

_COOKIE_FIELDS = {
    "netease": "netease_cookie",
    "qq": "qq_cookie",
    "kugou": "kugou_cookie",
}


@dataclass
class Settings:
    """全部可配置项。空字符串 / 空列表表示"未设置"，由调用方决定回退策略。"""

    db_path: str = ""
    music_roots: list[str] = field(default_factory=list)
    previous_roots: list[str] = field(default_factory=list)
    state_dir: str = "state"
    netease_cookie: str = ""
    qq_cookie: str = ""
    kugou_cookie: str = ""
    web_host: str = "127.0.0.1"
    web_port: int = 8765

    # ---------------------------------------------------------------- 读写

    def to_dict(self, *, mask_secrets: bool = False) -> dict[str, Any]:
        def secret(value: str) -> str:
            if not value:
                return ""
            if not mask_secrets:
                return value
            # 只保留前 8 个字符，避免把完整 Cookie 回显到浏览器
            head = value[:8]
            return f"{head}***" if len(value) > 12 else "***"

        return {
            "rekordbox": {
                "db_path": self.db_path,
                "music_roots": list(self.music_roots),
            },
            "paths": {"previous_roots": list(self.previous_roots)},
            "state_dir": self.state_dir,
            "netease_cookie": secret(self.netease_cookie),
            "qq_cookie": secret(self.qq_cookie),
            "kugou_cookie": secret(self.kugou_cookie),
            "web": {"host": self.web_host, "port": self.web_port},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        rekordbox = data.get("rekordbox") or {}
        paths = data.get("paths") or {}
        web = data.get("web") or {}
        music_roots = rekordbox.get("music_roots")
        if not isinstance(music_roots, list):
            music_roots = [music_roots] if music_roots else []
        previous_roots = paths.get("previous_roots")
        if not isinstance(previous_roots, list):
            previous_roots = [previous_roots] if previous_roots else []
        return cls(
            db_path=str(rekordbox.get("db_path") or ""),
            music_roots=[str(p) for p in music_roots if p],
            previous_roots=[str(p) for p in previous_roots if p],
            state_dir=str(data.get("state_dir") or "state"),
            netease_cookie=str(data.get("netease_cookie") or ""),
            qq_cookie=str(data.get("qq_cookie") or ""),
            kugou_cookie=str(data.get("kugou_cookie") or ""),
            web_host=str(web.get("host") or "127.0.0.1"),
            web_port=int(web.get("port") or 8765),
        )

    # ------------------------------------------------------------ 便捷方法

    def cookie(self, source: str) -> str:
        return getattr(self, _COOKIE_FIELDS.get(source, ""), "") or ""


def load_settings(path: str | Path = CONFIG_LOCAL) -> Settings:
    p = Path(path)
    if not p.is_file():
        return Settings()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return Settings()
    if not isinstance(data, dict):
        return Settings()
    settings = Settings.from_dict(data)

    # 兼容早期扁平写法：{"netease_cookie": "..."} 以及顶层 db_path
    if not settings.db_path and data.get("db_path"):
        settings.db_path = str(data["db_path"])
    if not settings.music_roots and data.get("music_roots"):
        roots = data["music_roots"]
        settings.music_roots = [str(r) for r in roots] if isinstance(roots, list) else [str(roots)]
    return settings


def save_settings(settings: Settings, path: str | Path = CONFIG_LOCAL) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = settings.to_dict()
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def update_settings(patch: dict[str, Any], path: str | Path = CONFIG_LOCAL) -> Settings:
    """只更新传入的字段，其余保持原样（避免前端整表覆盖丢掉 Cookie）。"""
    current = load_settings(path)
    for key, value in patch.items():
        if key == "rekordbox" and isinstance(value, dict):
            if "db_path" in value:
                current.db_path = str(value["db_path"] or "")
            if "music_roots" in value:
                roots = value["music_roots"] or []
                current.music_roots = [str(r) for r in roots] if isinstance(roots, list) else [str(roots)]
        elif key == "paths" and isinstance(value, dict):
            if "previous_roots" in value:
                roots = value["previous_roots"] or []
                current.previous_roots = [str(r) for r in roots] if isinstance(roots, list) else [str(roots)]
        elif key == "web" and isinstance(value, dict):
            if value.get("host"):
                current.web_host = str(value["host"])
            if value.get("port"):
                current.web_port = int(value["port"])
        elif key in ("netease_cookie", "qq_cookie", "kugou_cookie", "state_dir"):
            setattr(current, key, str(value or ""))
    save_settings(current, path)
    return current


def load_cookie(source: str, explicit: str | None = None) -> str:
    """Cookie 解析优先级：命令行 > 环境变量（<SOURCE>_COOKIE）> 配置文件。"""
    if explicit:
        return explicit.strip()
    env = os.environ.get(f"{source.upper()}_COOKIE", "").strip()
    if env:
        return env
    return load_settings().cookie(source)
