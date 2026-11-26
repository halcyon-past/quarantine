import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from quarantine import quarantine
from quarantine.ui import DashboardHandler, start_server


def test_ui_render_index(tmp_path):
    d = tmp_path / "q"

    @quarantine(dir=d)
    def broken():
        raise ValueError("Oops")

    broken()

    handler = DashboardHandler.__new__(DashboardHandler)
    DashboardHandler.quarantine_dir = d
    html = handler._render_index()

    assert "Oops" in html
    assert "ValueError" in html
    assert "broken" in html
    assert "#0001" in html


def test_ui_render_record(tmp_path):
    d = tmp_path / "q"

    @quarantine(dir=d)
    def broken(a):
        raise ValueError("Oops")

    broken(42)

    handler = DashboardHandler.__new__(DashboardHandler)
    DashboardHandler.quarantine_dir = d

    html = handler._render_record(1)
    assert "42" in html
    assert "Oops" in html
    assert "broken" in html

    assert handler._render_record(99) == "Record not found"


@pytest.fixture
def running_ui(tmp_path):
    d = tmp_path / "q"

    @quarantine(dir=d)
    def fail_fn(x):
        raise RuntimeError("failed")

    fail_fn(10)

    saved = DashboardHandler.quarantine_dir
    DashboardHandler.quarantine_dir = d
    server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        DashboardHandler.quarantine_dir = saved


def test_ui_get_record_validation(running_ui):
    base_url, _ = running_ui

    # Missing ID -> 400 Bad Request
    with pytest.raises(urllib.error.HTTPError) as exc_missing:
        urllib.request.urlopen(f"{base_url}/record")
    assert exc_missing.value.code == 400
    exc_missing.value.close()

    # Non-digit ID -> 400 Bad Request
    with pytest.raises(urllib.error.HTTPError) as exc_nondigit:
        urllib.request.urlopen(f"{base_url}/record?id=not_a_number")
    assert exc_nondigit.value.code == 400
    exc_nondigit.value.close()

    # Nonexistent record -> 404 Not Found
    with pytest.raises(urllib.error.HTTPError) as exc_404:
        urllib.request.urlopen(f"{base_url}/record?id=999")
    assert exc_404.value.code == 404
    exc_404.value.close()

    # Valid record -> 200 OK
    with urllib.request.urlopen(f"{base_url}/record?id=1") as resp:
        assert resp.status == 200
        content = resp.read().decode("utf-8")
        assert "fail_fn" in content


def test_ui_post_csrf_protection(running_ui):
    base_url, port = running_ui

    # Cross-origin attack with malicious Origin -> 403 Forbidden
    req = urllib.request.Request(
        f"{base_url}/retry?id=1",
        data=b"",
        headers={"Origin": "http://evil.com"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_origin:
        urllib.request.urlopen(req)
    assert exc_origin.value.code == 403
    exc_origin.value.close()

    # Cross-origin attack with malicious Referer -> 403 Forbidden
    req_ref = urllib.request.Request(
        f"{base_url}/retry?id=1",
        data=b"",
        headers={"Referer": "http://attacker.com/page"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_ref:
        urllib.request.urlopen(req_ref)
    assert exc_ref.value.code == 403
    exc_ref.value.close()

    # Malformed retry ID -> 400 Bad Request
    req_bad_id = urllib.request.Request(
        f"{base_url}/retry?id=invalid",
        data=b"",
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_bad_id:
        urllib.request.urlopen(req_bad_id)
    assert exc_bad_id.value.code == 400
    exc_bad_id.value.close()

    # Same-origin retry succeeds with 303 redirect
    req_good = urllib.request.Request(
        f"{base_url}/retry?id=1",
        data=b"",
        headers={"Origin": f"http://127.0.0.1:{port}"},
        method="POST",
    )
    # urlopen follows 303 redirects by default to / which returns 200
    with urllib.request.urlopen(req_good) as resp:
        assert resp.status == 200
        assert "Quarantine Dashboard" in resp.read().decode("utf-8")


def test_start_server_host_parameter(monkeypatch, tmp_path):
    calls = []

    class FakeServer:
        def __init__(self, addr, handler):
            calls.append(addr)

        def serve_forever(self):
            raise KeyboardInterrupt()

        def server_close(self):
            pass

    monkeypatch.setattr("http.server.ThreadingHTTPServer", FakeServer)
    ret = start_server(9999, tmp_path, host="127.0.0.1")
    assert ret == 0
    assert calls == [("127.0.0.1", 9999)]
