"""本机 HTTP 服务：静态页面 + JSON API。

安全（W1 已按"仅本机"收紧）：
- 只监听 127.0.0.1（由 CLI 默认值保证）；
- 校验 Host 头，挡住 DNS rebinding；
- 所有 /api/* 都要求 ``X-RB-Token``，token 每次启动随机生成并注入页面；
- 不返回 CORS 头，第三方网页读不到结果。
"""

from __future__ import annotations

import json
import mimetypes
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .api import Api, ApiError

STATIC_DIR = Path(__file__).parent / "static"
TOKEN_PLACEHOLDER = "__TOKEN_VALUE__"
VERSION_PLACEHOLDER = "__APP_VERSION__"
MAX_BODY = 1 << 20  # 1 MB，配置请求不可能更大


class App:
    def __init__(self, state_dir: str | Path = "state") -> None:
        self.state_dir = Path(state_dir)
        self.token = secrets.token_urlsafe(24)
        self.api = Api(self.state_dir)

    def render_index(self) -> bytes:
        from .. import __version__

        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        if TOKEN_PLACEHOLDER not in html:
            raise RuntimeError(
                f"index.html 缺少令牌占位符 {TOKEN_PLACEHOLDER}，页面将无法调用 API"
            )
        html = html.replace(TOKEN_PLACEHOLDER, self.token)
        # 把服务端版本写进页面：前端可以拿它和 /api/status 比对，识别"服务没重启"
        return html.replace(VERSION_PLACEHOLDER, __version__).encode("utf-8")


def make_handler(app: App):
    allowed_hosts_cache: dict[int, set[str]] = {}

    class Handler(BaseHTTPRequestHandler):
        server_version = "rb-sync"
        protocol_version = "HTTP/1.1"

        # ---------------------------------------------------------- 工具

        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            return  # 静音；真正的错误走 stderr

        def _allowed_hosts(self) -> set[str]:
            port = self.server.server_port
            if port not in allowed_hosts_cache:
                allowed_hosts_cache[port] = {
                    f"127.0.0.1:{port}",
                    f"localhost:{port}",
                    f"[::1]:{port}",
                }
            return allowed_hosts_cache[port]

        def _check_host(self) -> bool:
            host = (self.headers.get("Host") or "").strip().lower()
            if host in self._allowed_hosts():
                return True
            self._send_json(
                403,
                {
                    "error": {
                        "code": "bad_host",
                        "message": "只允许通过 127.0.0.1 访问",
                        "hint": "请用启动时打印的地址打开页面",
                    }
                },
            )
            return False

        def _check_token(self) -> bool:
            token = self.headers.get("X-RB-Token") or ""
            if secrets.compare_digest(token, app.token):
                return True
            self._send_json(
                403,
                {
                    "error": {
                        "code": "bad_token",
                        "message": "令牌无效",
                        "hint": "刷新页面重新获取令牌",
                    }
                },
            )
            return False

        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            if length > MAX_BODY:
                raise ApiError(413, "body_too_large", "请求体过大")
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ApiError(400, "bad_json", f"请求体不是合法 JSON：{exc}") from exc
            return data if isinstance(data, dict) else {}

        # ---------------------------------------------------------- 静态

        def _serve_static(self, path: str) -> None:
            if path in ("/", "/index.html"):
                self._send_bytes(200, app.render_index(), "text/html; charset=utf-8")
                return
            name = unquote(path.lstrip("/"))
            target = (STATIC_DIR / name).resolve()
            if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
                self._send_json(404, {"error": {"code": "not_found", "message": "文件不存在"}})
                return
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript",):
                ctype += "; charset=utf-8"
            self._send_bytes(200, target.read_bytes(), ctype)

        # ----------------------------------------------------------- HTTP

        def do_GET(self) -> None:  # noqa: N802
            if not self._check_host():
                return
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/"):
                if not self._check_token():
                    return
                status, payload = app.api.dispatch("GET", parsed.path, parse_qs(parsed.query), None)
                self._send_json(status, payload)
                return
            self._serve_static(parsed.path)

        def do_POST(self) -> None:  # noqa: N802
            self._mutate("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._mutate("PUT")

        def _mutate(self, method: str) -> None:
            if not self._check_host() or not self._check_token():
                return
            parsed = urlparse(self.path)
            try:
                body = self._read_json()
            except ApiError as exc:
                self._send_json(
                    exc.status, {"error": {"code": exc.code, "message": exc.message}}
                )
                return
            status, payload = app.api.dispatch(method, parsed.path, parse_qs(parsed.query), body)
            self._send_json(status, payload)

    return Handler


def serve(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    open_browser: bool = True,
    state_dir: str | Path = "state",
) -> None:
    app = App(state_dir)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    url = f"http://{host}:{port}/"

    print("rb-sync WebUI（W1：只读）")
    print(f"  地址 : {url}")
    print(f"  状态 : {Path(state_dir).resolve()}")
    print("  提示 : 关闭 Rekordbox 后才能写库；本版本不包含写库功能")
    print("  Ctrl+C 停止")
    print()

    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        print("已停止。")
