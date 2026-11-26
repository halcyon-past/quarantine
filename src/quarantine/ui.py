"""A tiny, zero-dependency local web dashboard to view quarantined records."""

import html
import http.server
import urllib.parse
from pathlib import Path

from . import api
from .reporting import emit, warn
from .store import default_dir


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    """Handles HTTP requests for the local quarantine dashboard."""

    quarantine_dir: Path | str | None = None

    def _validate_csrf(self) -> bool:
        """Validate Host, Origin, and Referer headers against DNS rebinding and CSRF."""
        host_header = self.headers.get("Host")
        if not host_header:
            return False

        addr = self.server.server_address
        if isinstance(addr, tuple):
            server_host, server_port = str(addr[0]), int(addr[1])
        else:
            server_host, server_port = "127.0.0.1", 8080
        allowed_hosts = {
            f"{server_host}:{server_port}",
            str(server_host),
            "127.0.0.1",
            f"127.0.0.1:{server_port}",
            "localhost",
            f"localhost:{server_port}",
        }
        if server_host in {"0.0.0.0", ""}:  # noqa: S104
            allowed_hosts.add(host_header.lower())
            allowed_hosts.add(host_header.split(":")[0].lower())
        elif host_header.lower() not in allowed_hosts:
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

    def do_GET(self) -> None:
        """Handle GET requests for viewing records."""
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in {"/", ""}:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(self._render_index().encode("utf-8"))
        elif parsed.path == "/record":
            query = urllib.parse.parse_qs(parsed.query)
            ids = query.get("id")
            if not ids or not ids[0].isdigit():
                self.send_response(400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Bad Request: missing or invalid record id")
                return
            record_id = int(ids[0])
            content = self._render_record(record_id)
            if content == "Record not found":
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Record not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(content.encode("utf-8"))
        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Not Found")

    def do_POST(self) -> None:
        """Handle POST requests for retrying records."""
        if not self._validate_csrf():
            self.send_response(403)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Forbidden: CSRF check failed")
            return

        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/retry":
            query = urllib.parse.parse_qs(parsed.query)
            ids = query.get("id")
            if not ids or not ids[0].isdigit():
                self.send_response(400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
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
            self.end_headers()
        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Not Found")

    def _render_index(self) -> str:
        records = api.records(dir=self.quarantine_dir)
        rows = []
        for r in records:
            rows.append(f"""
                <tr>
                    <td><a href="/record?id={r.id}">#{r.id:04d}</a></td>
                    <td><code>{html.escape(r.function)}</code></td>
                    <td>{html.escape(r.error_type)}</td>
                    <td>{html.escape(r.error[:50])}...</td>
                    <td>{r.attempts}</td>
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
    DashboardHandler.quarantine_dir = d if d else default_dir()
    server_address = (host, port)

    try:
        httpd = http.server.ThreadingHTTPServer(server_address, DashboardHandler)
        display_host = "localhost" if host == "127.0.0.1" else host
        emit(f"Quarantine UI running at http://{display_host}:{port}/")
        emit("Press Ctrl+C to stop.")
        httpd.serve_forever()
    except OSError as e:
        warn(f"Failed to start server on {host}:{port}: {e}")
        return 1
    except KeyboardInterrupt:
        emit("\nShutting down server...")
        httpd.server_close()

    return 0
