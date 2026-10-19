"""Repo-wide pytest policy (CLAUDE.md §7.5, §18): default test runs spend zero credits.

- Any non-loopback socket connect raises unless the test is marked @pytest.mark.live.
- @pytest.mark.live tests are skipped unless RUN_LIVE=1 or --run-live.
- @pytest.mark.milestone("M1") is recorded as a junit property so scripts/scoreboard.py can group results.
"""

from __future__ import annotations

import os
import socket
from typing import Any

import pytest

_LOOPBACK = {"127.0.0.1", "localhost", "::1", "0.0.0.0", "::"}


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--run-live", action="store_true", help="run @pytest.mark.live tests (spends credits)")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    live_ok = bool(config.getoption("--run-live")) or os.environ.get("RUN_LIVE") == "1"
    for item in items:
        m = item.get_closest_marker("milestone")
        if m and m.args:
            item.user_properties.append(("milestone", str(m.args[0])))
        if item.get_closest_marker("live") and not live_ok:
            item.add_marker(pytest.mark.skip(reason="live test: set RUN_LIVE=1 (spends credits)"))


@pytest.fixture(autouse=True)
def _block_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("live"):
        return
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _check(address: Any) -> None:
        if isinstance(address, tuple) and address and str(address[0]) not in _LOOPBACK:
            raise RuntimeError(
                f"network blocked in offline tests ({address!r}); use fakes/fixtures or mark @pytest.mark.live"
            )

    def connect(self: socket.socket, address: Any) -> None:
        _check(address)
        return real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        _check(address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
