"""A tiny, zero-dependency local web dashboard to view quarantined records."""

import html
import http.server
import socket
import urllib.parse
from pathlib import Path

from . import api
from .reporting import emit, warn
from .store import default_dir

_HTTP_DEFAULT_PORT = 80
_ERROR_PREVIEW_LEN = 50


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    """Handles HTTP requests for the local quarantine dashboard."""

    quarantine_dir: Path | str | None = None

    def _validate_host(self) -> tuple[bool, set[str]]:
        """Validate Host header against DNS rebinding and compute allowed origins."""
        host_header = self.headers.get("Host")
        if not host_header or any(ch.isspace() for ch in host_header):
            return False, set()

        addr = self.server.server_address
        if isinstance(addr, tuple):
            server_host, server_port = str(addr[0]), int(addr[1])
        else:
            server_host, server_port = "127.0.0.1", 8080

        if server_host in {"0.0.0.0", "", "::"}:  # noqa: S104
            return True, {host_header.lower()}

        formatted_host = (
            f"[{server_host}]"
            if ":" in server_host and not server_host.startswith("[")
            else server_host
        ).lower()
        hosts = {formatted_host, "127.0.0.1", "localhost", "[::1]"}
        allowed_hosts = {f"{h}:{server_port}" for h in hosts}
        if server_port == _HTTP_DEFAULT_PORT:
            allowed_hosts.update(hosts)

        if host_header.lower() not in allowed_hosts:
            return False, set()
        return True, allowed_hosts

    def _validate_csrf(self) -> bool:
        """Validate Host, Sec-Fetch-Site, Origin, and Referer headers."""
        ok, allowed_hosts = self._validate_host()
        if not ok:
            return False

        sec_fetch_site = self.headers.get("Sec-Fetch-Site")
        if sec_fetch_site and sec_fetch_site.lower() not in {"same-origin", "none"}:
            return False

        origin = self.headers.get("Origin")
        if origin:
            parsed_origin = urllib.parse.urlparse(origin).netloc.lower()
            if parsed_origin not in allowed_hosts:
                return False

        referer = self.headers.get("Referer")
        if referer:
            parsed_referer = urllib.parse.urlparse(referer).netloc.lower()
            if parsed_referer not in allowed_hosts:
                return False

        return True

    def _send_security_headers(self) -> None:
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")

    def do_GET(self) -> None:
        """Handle GET requests for viewing records."""
        ok, _ = self._validate_host()
        if not ok:
            self.send_response(403)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(b"Forbidden: invalid Host header")
            return

        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in {"/", ""}:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(self._render_index().encode("utf-8"))
        elif parsed.path == "/record":
            query = urllib.parse.parse_qs(parsed.query)
            ids = query.get("id")
            if not ids or not ids[0].isascii() or not ids[0].isdigit():
                self.send_response(400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self._send_security_headers()
                self.end_headers()
                self.wfile.write(b"Bad Request: missing or invalid record id")
                return
            record_id = int(ids[0])
            content = self._render_record(record_id)
            if content == "Record not found":
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self._send_security_headers()
                self.end_headers()
                self.wfile.write(b"Record not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(content.encode("utf-8"))
        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(b"Not Found")

    def do_POST(self) -> None:
        """Handle POST requests for retrying records."""
        if not self._validate_csrf():
            self.send_response(403)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(b"Forbidden: CSRF check failed")
            return

        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/retry":
            query = urllib.parse.parse_qs(parsed.query)
            ids = query.get("id")
            if not ids or not ids[0].isascii() or not ids[0].isdigit():
                self.send_response(400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self._send_security_headers()
                self.end_headers()
                self.wfile.write(b"Bad Request: missing or invalid record id")
                return

            record_id = int(ids[0])
            try:
                api.retry([record_id], dir=self.quarantine_dir)
            except Exception as exc:  # noqa: BLE001 - generic failure
                warn(f"Error during retry: {exc}")

            self.send_response(303)
            self.send_header("Location", "/")
            self._send_security_headers()
            self.end_headers()
        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(b"Not Found")

    def _render_index(self) -> str:
        records = api.records(dir=self.quarantine_dir)
        rows = []
        for r in records:
            preview_err = r.error[:_ERROR_PREVIEW_LEN] + (
                "..." if len(r.error) > _ERROR_PREVIEW_LEN else ""
            )
            rows.append(f"""
                <tr>
                    <td><a href="/record?id={r.id}">#{r.id:04d}</a></td>
                    <td><code>{html.escape(r.function)}</code></td>
                    <td>{html.escape(r.error_type)}</td>
                    <td>{html.escape(preview_err)}</td>
                    <td>{html.escape(str(r.attempts))}</td>
                    <td>
                        <form method="POST" action="/retry?id={r.id}" style="margin:0;">
                            <button type="submit" class="retry-btn">Retry</button>
                        </form>
                    </td>
                </tr>
            """)

        return f"""<!DOCTYPE html>
<html>
<head>
    <title>Quarantine Dashboard</title>
    <style>
        body {{ font-family: sans-serif; margin: 40px; color: #333; }}
        h1 {{ border-bottom: 2px solid #eaecef; padding-bottom: 10px; }}
        table {{ border-collapse: collapse; width: 100%; }}
        th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
        th {{ background-color: #f6f8fa; }}
        tr:hover {{ background-color: #f1f1f1; }}
        .retry-btn {{ background: #28a745; color: white; border: none; padding: 5px; }}
        .retry-btn:hover {{ background: #218838; }}
        .empty {{ text-align: center; color: #666; margin-top: 50px; }}
    </style>
</head>
<body>
    <h1>Quarantine Dashboard</h1>
    {
            '<p class="empty">No quarantined records found.</p>'
            if not rows
            else f'''
    <table>
        <tr>
            <th>ID</th>
            <th>Function</th>
            <th>Error Type</th>
            <th>Message</th>
            <th>Attempts</th>
            <th>Action</th>
        </tr>
        {"".join(rows)}
    </table>
    '''
        }
</body>
</html>"""

    def _render_record(self, record_id: int) -> str:
        records = api.records(dir=self.quarantine_dir)
        record = next((r for r in records if r.id == record_id), None)
        if not record:
            return "Record not found"

        input_text = html.escape(record.input_text())
        traceback = html.escape(record.traceback_text())

        return f"""<!DOCTYPE html>
<html>
<head>
    <title>Record #{record.id:04d}</title>
    <style>
        body {{ font-family: sans-serif; margin: 40px; color: #333; }}
        h1 {{ border-bottom: 2px solid #eaecef; padding-bottom: 10px; }}
        pre {{ background: #f6f8fa; padding: 16px; border-radius: 6px; overflow: auto; }}
        .back {{ display: inline-block; margin-bottom: 20px; text-decoration: none; }}
    </style>
</head>
<body>
    <a href="/" class="back">&larr; Back to Dashboard</a>
    <h1>Record #{record.id:04d}: {html.escape(record.function)}</h1>

    <h2>Input Payload</h2>
    <pre>{input_text}</pre>

    <h2>Traceback</h2>
    <pre>{traceback}</pre>
</body>
</html>"""


def start_server(port: int, d: Path | str | None = None, host: str = "127.0.0.1") -> int:
    """Start the lightweight HTTP dashboard."""
    target_dir = d if d else default_dir()

    class _BoundDashboardHandler(DashboardHandler):
        quarantine_dir = target_dir

    server_address = (host, port)
    httpd: http.server.ThreadingHTTPServer | None = None
    server_cls = http.server.ThreadingHTTPServer
    if ":" in host and server_cls is http.server.ThreadingHTTPServer:

        class _IPv6ThreadingHTTPServer(http.server.ThreadingHTTPServer):
            address_family = socket.AF_INET6

        server_cls = _IPv6ThreadingHTTPServer

    try:
        httpd = server_cls(server_address, _BoundDashboardHandler)
        bound_addr = getattr(httpd, "server_address", server_address)
        bound_port = int(bound_addr[1]) if isinstance(bound_addr, tuple) else port
        display_host = "localhost" if host == "127.0.0.1" else host
        if ":" in display_host and not display_host.startswith("["):
            display_host = f"[{display_host}]"
        emit(f"Quarantine UI running at http://{display_host}:{bound_port}/")
        emit("Press Ctrl+C to stop.")
        httpd.serve_forever()
    except OSError as e:
        warn(f"Failed to start server on {host}:{port}: {e}")
        return 1
    except KeyboardInterrupt:
        emit("\nShutting down server...")
        if httpd is not None:
            httpd.server_close()

    return 0
