from quarantine import quarantine
from quarantine.ui import DashboardHandler


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
