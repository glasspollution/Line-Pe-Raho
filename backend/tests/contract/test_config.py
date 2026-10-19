"""Contract: app.config (CLAUDE.md §17, §21). Task T-001.

Pinned interface:
    Settings(**overrides) — pydantic-settings, reads .env; field names are the lower-cased env names.
    gnani_api_key is a SecretStr (never appears in repr/str/model_dump_json).
"""

from __future__ import annotations

from types import ModuleType

import pytest

from tests.contract.helpers import require

pytestmark = pytest.mark.milestone("M0")
FAKE_KEY = "vach_" + "x" * 30  # guard: allow secret-gnani -- fake key for redaction test


@pytest.fixture(scope="module")
def cfg() -> ModuleType:
    return require("app.config", "T-001")


def test_defaults_without_env_file(cfg: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GNANI_API_KEY", raising=False)
    s = cfg.Settings(_env_file=None, gnani_api_key="k")
    assert s.gnani_base_url == "https://api.vachana.ai"
    assert s.line_kind == "virtual"
    assert s.hold_stt_mode in ("stream_all", "gated")


def test_secret_never_leaks(cfg: ModuleType) -> None:
    s = cfg.Settings(_env_file=None, gnani_api_key=FAKE_KEY)
    assert FAKE_KEY not in repr(s) and FAKE_KEY not in str(s)
    assert FAKE_KEY not in s.model_dump_json()
    assert s.gnani_api_key.get_secret_value() == FAKE_KEY


def test_llm_endpoint_from_env(cfg: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:9999/v1")
    monkeypatch.setenv("LLM_MODEL", "evon-test")
    s = cfg.Settings(_env_file=None, gnani_api_key="k")
    assert (s.llm_base_url, s.llm_model) == ("http://127.0.0.1:9999/v1", "evon-test")
