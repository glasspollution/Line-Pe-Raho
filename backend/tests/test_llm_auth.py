"""BearerTokenSource: api_key passthrough, offline Bedrock token minting, caching, no leaks."""

from __future__ import annotations

from typing import Any

import pytest
from botocore.credentials import Credentials

from app.config import Settings
from app.llm.auth import BearerTokenSource

FAKE_ACCESS = "AKIA" + "TESTTESTTESTTEST"  # guard: allow secret-aws -- fake static credential for an offline test


class StaticProvider:
    def __init__(self) -> None:
        self.calls = 0

    def load(self) -> Any:
        self.calls += 1
        return Credentials(FAKE_ACCESS, "fake-secret-key")


def settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, gnani_api_key="k", **kw)


def test_api_key_mode() -> None:
    src = BearerTokenSource(settings(llm_auth="api_key", llm_api_key="sk-test"))
    assert src.get() == "sk-test"


def test_api_key_mode_requires_key() -> None:
    with pytest.raises(RuntimeError):
        BearerTokenSource(settings(llm_auth="api_key", llm_api_key="")).get()


def test_bedrock_token_is_minted_offline_and_cached() -> None:
    prov = StaticProvider()
    src = BearerTokenSource(settings(llm_auth="bedrock_profile", aws_region="us-east-1"), credential_provider=prov)
    tok = src.get()
    assert tok.startswith("bedrock-api-key-") and len(tok) > 100
    assert src.get() == tok  # cached
    assert prov.calls == 1
    assert "bedrock-api-key" not in repr(src) and FAKE_ACCESS not in repr(src)
