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

# 最近一次 load_settings 的问题（解析失败 / 读不了）。
# 以前是静默回退默认值 —— 手改配置少个逗号就会表现为"功能全坏"，所以现在把它暴露出来。
_LAST_ERROR: str = ""


def config_error() -> str:
    """最近一次读配置时出的错（空字符串表示没问题）。"""
    return _LAST_ERROR

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
    # fetch 阶段：每个平台一条外部下载命令模板（项目本身不下载）
    fetch_commands: dict[str, str] = field(default_factory=dict)
    # 下载成功后的通用后处理（例如用你自己的工具把加密容器转成可播放文件）
    fetch_post_command: str = ""
    fetch_out_dir: str = ""
    fetch_timeout: int = 600
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
            "fetch": {
                "commands": dict(self.fetch_commands),
                "post_command": self.fetch_post_command,
                "out_dir": self.fetch_out_dir,
                "timeout": self.fetch_timeout,
            },
            "netease_cookie": secret(self.netease_cookie),
            "qq_cookie": secret(self.qq_cookie),
            "kugou_cookie": secret(self.kugou_cookie),
            "web": {"host": self.web_host, "port": self.web_port},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        rekordbox = data.get("rekordbox") or {}
        paths = data.get("paths") or {}
        fetch = data.get("fetch") or {}
        commands = fetch.get("commands")
        if not isinstance(commands, dict):
            commands = {}
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
            fetch_commands={str(k): str(v) for k, v in commands.items() if v},
            fetch_post_command=str(fetch.get("post_command") or ""),
            fetch_out_dir=str(fetch.get("out_dir") or ""),
            fetch_timeout=int(fetch.get("timeout") or 600),
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
    global _LAST_ERROR
    _LAST_ERROR = ""
    p = Path(path)
    if not p.is_file():
        return Settings()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _LAST_ERROR = f"{p.name} 第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}"
        return Settings()
    except OSError as exc:
        _LAST_ERROR = f"{p.name} 读取失败：{exc}"
        return Settings()
    if not isinstance(data, dict):
        _LAST_ERROR = f"{p.name} 顶层必须是一个对象 {{...}}"
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
    if config_error():
        # 配置已经坏了，不能再往上叠加写 —— 否则会把坏的整份写回去
        raise ValueError(
            f"配置文件无法解析（{config_error()}）。先修好它，或删掉 config.local.json 重新配置。"
        )
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
        elif key == "fetch" and isinstance(value, dict):
            if isinstance(value.get("commands"), dict):
                merged = dict(current.fetch_commands)
                for source, template in value["commands"].items():
                    text = str(template or "").strip()
                    if text:
                        merged[str(source)] = text
                    else:
                        merged.pop(str(source), None)  # 传空字符串 = 删除该平台的命令
                current.fetch_commands = merged
            if "post_command" in value:
                current.fetch_post_command = str(value["post_command"] or "")
            if "out_dir" in value:
                current.fetch_out_dir = str(value["out_dir"] or "")
            if value.get("timeout"):
                current.fetch_timeout = int(value["timeout"])
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
