"""The constraint guard must catch each class of §0 violation and stay quiet on clean code."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import guard_constraints as G

ALLOW = {"9000000001"}


def errors(text: str, rel: str = "backend/app/x.py") -> list[str]:
    return [f.rule for f in G.scan_text(rel, text, rel.endswith((".py", ".ts", ".tsx", ".json")), ALLOW)
            if f.level == "error"]


@pytest.mark.parametrize("line", [
    "import whisper",
    "from elevenlabs import generate",
    "from google.cloud import speech  # google.cloud.speech",
    "client.audio.transcriptions.create(file=f)",
    "URL = 'https://api.deepgram.com/v1/listen'",
    "const r = new webkitSpeechRecognition()",
    "window.speechSynthesis.speak(u)",
    '"@elevenlabs/react": "^0.5.0",',
], ids=["py-import", "py-from-import", "cloud-sdk", "openai-audio", "host", "browser-stt", "browser-tts", "npm-sdk"])
def test_flags_non_gnani_speech(line: str) -> None:
    assert any(r.startswith("provider") for r in errors(line)), line


def test_provider_names_in_markdown_are_fine() -> None:
    assert errors("We considered whisper but §0 forbids it.", rel="docs/notes.md") == []


def test_flags_hardcoded_llm_host() -> None:
    assert "llm-hardcoded" in errors("base_url='https://api.openai.com/v1'")


@pytest.mark.parametrize("text", ["Welcome to Airtel", "SBI customer care", "hdfc bank", "Pay via PhonePe"],
                         ids=["telecom", "bank-acronym", "bank-lower", "wallet"])
def test_flags_real_brands(text: str) -> None:
    assert "real-brand" in errors(text, rel="backend/app/company/trees/x.yaml")


def test_fictional_brands_and_tailwind_ok() -> None:
    assert errors("Nimbus Telecom, Sahyog Bank, Prakash Bijli Board; className='bg-indigo-500'") == []


@pytest.mark.parametrize("text", [
    "KEY = 'vach_" + "A" * 40 + "'",
    "api_key = 'abcdefghijklmnopqrstuvwxyz123456'",
    "-----BEGIN RSA PRIVATE KEY-----",
], ids=["gnani-key", "assigned-key", "pem"])
def test_flags_secrets(text: str) -> None:
    assert any(r.startswith("secret") for r in errors(text))


def test_mobile_numbers_must_be_allowlisted() -> None:
    assert "pii-mobile" in errors("profile = {'registered_mobile': '98" + "45612307'}")
    assert errors("profile = {'registered_mobile': '9000000001'}") == []
    assert errors("t = 0.9876543210") == []  # floats are not phone numbers
    assert errors("ticket NMB48213377") == []


def test_inline_allow_suppresses_one_rule() -> None:
    line = "brand = 'Airtel'  # guard: allow real-brand -- test fixture for guard"
    assert errors(line) == []


def test_repo_is_clean() -> None:
    root = Path(__file__).resolve().parents[2]
    found = [f for f in G.run(root) if f.level == "error"]
    assert not found, "\n".join(f"{f.rule} {f.path}:{f.line} {f.message}" for f in found)


def test_env_file_is_ignored_when_present(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".env").write_text("GNANI_API_KEY=x\n")
    rules = {f.rule for f in G.check_env_hygiene(tmp_path)}
    assert "env-not-ignored" in rules
    (tmp_path / ".gitignore").write_text(".env\n")
    assert G.check_env_hygiene(tmp_path) == []
