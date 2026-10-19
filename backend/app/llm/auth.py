"""Credentials for the OpenAI-compatible LLM endpoint (CLAUDE.md §0.2, §9.4, D-4).

llm_auth = "api_key"         -> use LLM_API_KEY as the bearer token (any OpenAI-compatible provider)
llm_auth = "bedrock_profile" -> mint a short-lived Bedrock bearer token from a local AWS profile
                                (SigV4 presign, done locally; no long-lived key in .env)

Tokens are never logged or returned in reprs.
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta
from typing import Any

from app.config import Settings

_REFRESH_MARGIN_S = 300.0


class _ProfileCredentialProvider:
    """Adapts a botocore session's credentials to the token generator's provider interface."""

    def __init__(self, profile: str) -> None:
        import botocore.session

        self._session = botocore.session.Session(profile=profile or None)

    def load(self) -> Any:
        creds = self._session.get_credentials()
        if creds is None:
            raise RuntimeError("no AWS credentials found for the configured profile")
        return creds


class BearerTokenSource:
    """Thread-safe cached bearer token for the LLM client."""

    def __init__(self, settings: Settings, credential_provider: Any | None = None) -> None:
        self._s = settings
        self._provider = credential_provider
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"BearerTokenSource(auth={self._s.llm_auth!r})"

    def get(self) -> str:
        if self._s.llm_auth == "api_key":
            key = self._s.llm_api_key.get_secret_value()
            if not key:
                raise RuntimeError("LLM_API_KEY is empty (llm_auth=api_key)")
            return key
        with self._lock:
            now = time.time()
            if self._token is None or now >= self._expires_at - _REFRESH_MARGIN_S:
                from aws_bedrock_token_generator import provide_token

                provider = self._provider or _ProfileCredentialProvider(self._s.aws_profile)
                ttl = self._s.llm_token_ttl_s
                self._token = provide_token(region=self._s.aws_region, aws_credentials_provider=provider,
                                            expiry=timedelta(seconds=ttl))
                self._expires_at = now + ttl
            return self._token
