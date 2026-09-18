"""Tests for systemd timer and service unit templates."""

from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


# --- autoloop-eval.service ---


def test_service_file_exists():
    assert (TEMPLATES_DIR / "autoloop-eval.service").exists()


def test_service_has_unit_section():
    content = (TEMPLATES_DIR / "autoloop-eval.service").read_text()
    assert "[Unit]" in content


def test_service_has_service_section():
    content = (TEMPLATES_DIR / "autoloop-eval.service").read_text()
    assert "[Service]" in content


def test_service_type_oneshot():
    content = (TEMPLATES_DIR / "autoloop-eval.service").read_text()
    assert "Type=oneshot" in content


def test_service_exec_start_autoloop_eval_publish():
    content = (TEMPLATES_DIR / "autoloop-eval.service").read_text()
    assert "ExecStart=" in content
    assert "autoloop eval --publish" in content


def test_service_exec_start_resolvable_path():
    content = (TEMPLATES_DIR / "autoloop-eval.service").read_text()
    for line in content.splitlines():
        if line.startswith("ExecStart="):
            path = line.split("=", 1)[1].split()[0]
            assert path.startswith("/") or path.startswith("%h/")
            break
    else:
        raise AssertionError("No ExecStart line found")


def test_service_has_install_section():
    content = (TEMPLATES_DIR / "autoloop-eval.service").read_text()
    assert "[Install]" in content


# --- autoloop-eval.timer ---


def test_timer_file_exists():
    assert (TEMPLATES_DIR / "autoloop-eval.timer").exists()


def test_timer_has_unit_section():
    content = (TEMPLATES_DIR / "autoloop-eval.timer").read_text()
    assert "[Unit]" in content


def test_timer_has_timer_section():
    content = (TEMPLATES_DIR / "autoloop-eval.timer").read_text()
    assert "[Timer]" in content


def test_timer_on_calendar_daily_0500():
    content = (TEMPLATES_DIR / "autoloop-eval.timer").read_text()
    assert "OnCalendar=*-*-* 05:00:00" in content


def test_timer_persistent():
    content = (TEMPLATES_DIR / "autoloop-eval.timer").read_text()
    assert "Persistent=true" in content


def test_timer_binds_to_service():
    content = (TEMPLATES_DIR / "autoloop-eval.timer").read_text()
    assert "Unit=autoloop-eval.service" in content


def test_timer_has_install_section():
    content = (TEMPLATES_DIR / "autoloop-eval.timer").read_text()
    assert "[Install]" in content


def test_timer_documents_schedule():
    content = (TEMPLATES_DIR / "autoloop-eval.timer").read_text()
    lines = content.splitlines()
    comment_lines = [line for line in lines if line.startswith("#")]
    assert any("05:00" in c for c in comment_lines)
