"""`ul8 health` runs the repo suite and reports the pass count (T1.4).

The health check is the one place a user sees the whole tool at once, so it
should also answer "is this tree passing". The suite is slow (~50s), so the
gating has to be right in two directions: a failing suite is a failed check,
while a suite that cannot run at all (no pytest, no tests/ dir, we are already
inside pytest) is a note that must NOT be reported as a pass.

The recursion guard is the sharp edge: `run_health_check()` calls
`run_test_suite()`, so a test that calls the health check would spawn a second
pytest run, which would call the health check again. `PYTEST_CURRENT_TEST` is
set by pytest itself, so it is the honest signal and it is tested here.
"""

from __future__ import annotations

import subprocess

import pytest

import hardware_guide


def _proc(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["pytest"], returncode=returncode,
                                       stdout=stdout, stderr="")


@pytest.fixture
def outside_pytest(monkeypatch):
    """Pretend the health check is not being run from inside the suite.

    Patching the signal, not the environment: pytest rewrites
    PYTEST_CURRENT_TEST after fixtures are set up, so deleting it here would
    not survive into the test body.
    """
    monkeypatch.setattr(hardware_guide, "inside_pytest", lambda: False)
    monkeypatch.delenv("UL8_NO_TESTS", raising=False)


@pytest.fixture
def spawn(monkeypatch):
    """Stub the pytest launch and hand back the call recorder."""
    calls = []

    def fake(argv):
        calls.append(argv)
        return _proc("388 passed in 48.84s\n")

    monkeypatch.setattr(hardware_guide, "_run_pytest", fake)
    return calls


# ── summary parsing ─────────────────────────────────────────────────


def test_parses_clean_pass():
    assert hardware_guide.parse_pytest_counts("388 passed in 48.84s") == {"passed": 388}


def test_parses_failures_errors_and_skips():
    counts = hardware_guide.parse_pytest_counts("2 failed, 5 skipped, 1 xfailed, 380 passed, 3 errors in 51.00s")
    assert counts == {"failed": 2, "skipped": 5, "xfailed": 1, "passed": 380, "errors": 3}


def test_last_match_per_word_wins():
    # pytest -q prints the summary last; a count quoted earlier (a test's own
    # output) must not override it.
    counts = hardware_guide.parse_pytest_counts("assert '3 failed' == '0 failed'\n1 failed, 387 passed in 50.00s")
    assert counts["failed"] == 1


def test_parses_nothing_out_of_prose():
    assert hardware_guide.parse_pytest_counts("no tests ran") == {}


# ── the run itself ──────────────────────────────────────────────────


def test_clean_suite_reads_as_ok(outside_pytest, spawn):
    result = hardware_guide.run_test_suite()
    assert result == {"ran": True, "ok": True, "detail": "388 passed in 48.84s"}
    assert len(spawn) == 1
    assert "-p" in spawn[0] and "no:cacheprovider" in spawn[0]


def test_failing_suite_reads_as_failed(outside_pytest, monkeypatch):
    monkeypatch.setattr(hardware_guide, "_run_pytest",
                        lambda argv: _proc("1 failed, 387 passed in 51.00s\n", returncode=1))
    result = hardware_guide.run_test_suite()
    assert result["ran"] is True
    assert result["ok"] is False
    assert result["detail"] == "387 passed, 1 failed in 51.00s"


def test_error_count_is_not_a_pass(outside_pytest, monkeypatch):
    # rc can be 1 with only "errors" in the summary: still not a pass.
    monkeypatch.setattr(hardware_guide, "_run_pytest",
                        lambda argv: _proc("2 errors in 3.00s\n", returncode=1))
    result = hardware_guide.run_test_suite()
    assert result["ok"] is False
    assert result["detail"] == "2 errors in 3.00s"


def test_unparsable_run_is_not_a_pass(outside_pytest, monkeypatch):
    monkeypatch.setattr(hardware_guide, "_run_pytest",
                        lambda argv: _proc("no tests ran in 0.01s\n", returncode=5))
    result = hardware_guide.run_test_suite()
    assert result["ran"] is True
    assert result["ok"] is False
    assert result["detail"] == "pytest exited 5 with no summary"


def test_launch_failure_is_a_note(outside_pytest, monkeypatch):
    def boom(argv):
        raise OSError("no such file")

    monkeypatch.setattr(hardware_guide, "_run_pytest", boom)
    result = hardware_guide.run_test_suite()
    assert result["ran"] is False
    assert result["detail"].startswith("could not run pytest: no such file")


# ── the guards ──────────────────────────────────────────────────────


def test_inside_pytest_is_actually_true_here():
    # the guard's signal has to be real: this suite is running under pytest
    assert hardware_guide.inside_pytest() is True


def test_never_recurses_from_inside_pytest(monkeypatch):
    monkeypatch.setattr(hardware_guide, "inside_pytest", lambda: True)
    monkeypatch.setattr(hardware_guide, "_run_pytest",
                        lambda argv: pytest.fail("recursed into the suite"))
    result = hardware_guide.run_test_suite()
    assert result["ran"] is False
    assert "inside pytest" in result["detail"]


def test_ul8_no_tests_skips(outside_pytest, monkeypatch):
    monkeypatch.setenv("UL8_NO_TESTS", "1")
    monkeypatch.setattr(hardware_guide, "_run_pytest",
                        lambda argv: pytest.fail("ran the suite with UL8_NO_TESTS=1"))
    result = hardware_guide.run_test_suite()
    assert result["ran"] is False
    assert "UL8_NO_TESTS=1" in result["detail"]


def test_missing_tests_dir_is_a_note(outside_pytest, monkeypatch, tmp_path):
    monkeypatch.setattr(hardware_guide, "TESTS_DIR", tmp_path / "nope")
    monkeypatch.setattr(hardware_guide, "_run_pytest",
                        lambda argv: pytest.fail("ran without a tests/ dir"))
    result = hardware_guide.run_test_suite()
    assert result == {"ran": False, "ok": False, "detail": "no tests/ in this tree"}


def test_missing_pytest_is_a_note(outside_pytest, monkeypatch):
    monkeypatch.setattr(hardware_guide, "pytest_available", lambda: False)
    monkeypatch.setattr(hardware_guide, "_run_pytest",
                        lambda argv: pytest.fail("ran without pytest installed"))
    result = hardware_guide.run_test_suite()
    assert result == {"ran": False, "ok": False, "detail": "pytest not installed"}


# ── the health check row ────────────────────────────────────────────


@pytest.fixture
def quiet_health(monkeypatch, tmp_path):
    """Make every other health row pass so only the suite row can fail."""
    import img4wrap
    import pwn_utils
    import toolchain

    (tmp_path / "usbliter8ctl").write_text("x")
    monkeypatch.setattr(hardware_guide, "_load_config", lambda: {"selected_board": "pico2"})
    monkeypatch.setattr(hardware_guide, "check_firmware", lambda board_id: True)
    monkeypatch.setattr(toolchain, "TOOLS_DIR", tmp_path)
    monkeypatch.setattr(toolchain, "tool_available", lambda name: True)
    monkeypatch.setattr(pwn_utils, "check_pyusb_installed", lambda: True)
    monkeypatch.setattr(pwn_utils, "usb_status",
                        lambda: {"ready": True, "backend": "libusb", "problem": None})
    monkeypatch.setattr(pwn_utils, "detect_rp2350", lambda: {"bus": 1, "address": 2})
    monkeypatch.setattr(img4wrap, "decoder_available", lambda: (True, "pyimg4"))


def test_health_row_reports_the_pass_count(quiet_health, monkeypatch, capsys):
    monkeypatch.setattr(hardware_guide, "run_test_suite",
                        lambda: {"ran": True, "ok": True, "detail": "388 passed in 48.8s"})
    results = hardware_guide.run_health_check()
    out = capsys.readouterr().out
    assert "Test suite" in out
    assert "388 passed in 48.8s" in out
    assert results["test_suite"] is True


def test_failing_suite_fails_the_health_verdict(quiet_health, monkeypatch, capsys):
    monkeypatch.setattr(hardware_guide, "run_test_suite",
                        lambda: {"ran": True, "ok": False, "detail": "1 failed, 387 passed in 51s"})
    results = hardware_guide.run_health_check()
    out = capsys.readouterr().out
    assert results["test_suite"] is False
    assert "test_suite" in out or "Test suite" in out
    assert "All checks passed" not in out


def test_skipped_suite_does_not_count_as_a_pass(quiet_health, monkeypatch, capsys):
    monkeypatch.setattr(hardware_guide, "run_test_suite",
                        lambda: {"ran": False, "ok": False, "detail": "pytest not installed"})
    results = hardware_guide.run_health_check()
    out = capsys.readouterr().out
    assert "test_suite" not in results
    assert "pytest not installed" in out
    # every other row passed, so the verdict stays "ready to exploit"
    assert "All checks passed" in out
