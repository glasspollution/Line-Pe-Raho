"""The one-command end-to-end run (offline) must succeed: plan -> call -> verified summary matching truth."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import e2e


@pytest.mark.milestone("M3")
@pytest.mark.parametrize("company,text", [
    ("nimbus_telecom", "Nimbus ne mera recharge do baar kaat liya, refund chahiye"),
    ("sahyog_bank", "My card transaction failed but the money was debited"),
    ("prakash_bijli_board", "Hamare area mein subah se bijli nahi hai"),
])
async def test_offline_e2e(tmp_path: Path, company: str, text: str) -> None:
    assert await e2e.offline(text, company, tmp_path) == 0
    assert (tmp_path / "summary.json").exists() and (tmp_path / "recordings" / "mixed.wav").exists()
