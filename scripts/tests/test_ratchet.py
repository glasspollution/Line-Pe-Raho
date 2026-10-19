"""The ratchet must treat failed, skipped and missing as regressions, and only ever grow."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from scripts import ratchet as R

JUNIT = """<?xml version="1.0"?>
<testsuites><testsuite name="pytest">
  <testcase classname="a.test_x" name="test_ok"/>
  <testcase classname="a.test_x" name="test_bad"><failure message="boom"/></testcase>
  <testcase classname="a.test_x" name="test_skip"><skipped message="NOT IMPLEMENTED: app.x [T-1]"/></testcase>
  <testcase classname="a.test_x" name="test_new"/>
</testsuite></testsuites>
"""


def _junit(tmp: Path) -> Path:
    p = tmp / "junit.xml"
    p.write_text(JUNIT)
    return p


def test_read_junit_statuses(tmp_path: Path) -> None:
    res = R.read_junit(_junit(tmp_path))
    assert res == {"a.test_x::test_ok": "passed", "a.test_x::test_bad": "failed",
                   "a.test_x::test_skip": "skipped", "a.test_x::test_new": "passed"}


def test_regressions_include_failed_skipped_missing(tmp_path: Path) -> None:
    res = R.read_junit(_junit(tmp_path))
    locked = ["a.test_x::test_ok", "a.test_x::test_bad", "a.test_x::test_skip", "a.test_x::test_gone"]
    regs = dict(R.regressions(locked, res))
    assert regs == {"a.test_x::test_bad": "failed", "a.test_x::test_skip": "skipped",
                    "a.test_x::test_gone": "missing"}


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    script = Path(R.__file__)
    return subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True)


def test_update_only_adds(tmp_path: Path) -> None:
    rpath = tmp_path / "ratchet.json"
    R.save(rpath, ["a.test_x::test_ok"])
    p = _cli("--junit", str(_junit(tmp_path)), "--ratchet", str(rpath), "--update")
    assert p.returncode == 0, p.stdout
    assert R.load(rpath) == ["a.test_x::test_new", "a.test_x::test_ok"]


def test_check_fails_on_regression(tmp_path: Path) -> None:
    rpath = tmp_path / "ratchet.json"
    R.save(rpath, ["a.test_x::test_bad"])
    assert _cli("--junit", str(_junit(tmp_path)), "--ratchet", str(rpath)).returncode == 1


def test_base_detects_removed_ids(tmp_path: Path) -> None:
    base, cur = tmp_path / "base.json", tmp_path / "cur.json"
    R.save(base, ["a.test_x::test_ok", "a.test_x::test_new"])
    R.save(cur, ["a.test_x::test_ok"])
    p = _cli("--junit", str(_junit(tmp_path)), "--ratchet", str(cur), "--base", str(base))
    assert p.returncode == 1 and "removed" in p.stdout
