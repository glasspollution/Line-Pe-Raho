"""Deny-lists for scripts/guard_constraints.py (CLAUDE.md §0 hard constraints).

This file is exempt from scanning because it necessarily names what it forbids.
Edit these lists only via a PR that explains why (the critic checks this).
"""

from __future__ import annotations

CODE_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json", ".toml", ".yaml", ".yml", ".txt", ".html"}
TEXT_EXT = CODE_EXT | {".md", ".jsonl", ".csv", ".env", ".example", ".cfg", ".ini", ".sh", ".ps1"}

# Never scanned (generated, vendored, or the rule files themselves).
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build", "data", "reports",
             "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache", "test-results",
             "playwright-report"}
SKIP_FILES = {"scripts/guard_rules.py", "scripts/tests/test_guard.py", "CLAUDE.md",
              "eval/ratchet.json"}  # generated test ids; may quote rule strings from test_guard

# §0.1 — speech must be Gnani only. (rule_id, regex, message). Applied to CODE_EXT files.
SPEECH_PROVIDER_RULES: list[tuple[str, str, str]] = [
    ("provider-import", r"^\s*(?:from|import)\s+(?:whisper|faster_whisper|whisperx|deepgram|assemblyai|"
     r"elevenlabs|gtts|pyttsx3|speech_recognition|vosk|sarvamai|riva|TTS\.api|edge_tts)\b",
     "non-Gnani STT/TTS library imported"),
    ("provider-import", r"google\.cloud\.(?:speech|texttospeech)|azure\.cognitiveservices\.speech",
     "non-Gnani cloud speech SDK"),
    ("provider-import", r"client\(\s*['\"](?:polly|transcribe)['\"]", "AWS speech service client"),
    ("provider-api", r"\.audio\.(?:transcriptions|speech|translations)\b", "OpenAI-style audio endpoint"),
    ("provider-host", r"api\.deepgram\.com|api\.assemblyai\.com|speech\.googleapis\.com|"
     r"texttospeech\.googleapis\.com|api\.elevenlabs\.io|api\.sarvam\.ai|bhashini\.gov|"
     r"cognitiveservices\.azure\.com|api\.openai\.com/v1/audio",
     "non-Gnani speech API host"),
    ("provider-npm", r"\"@(?:elevenlabs|deepgram|google-cloud|azure|aws-sdk/client-(?:polly|transcribe))[/\w-]*\"\s*:",
     "frontend depends on a speech provider SDK (visual-only components are fine, SDKs are not)"),
    ("provider-npm", r"\"(?:livekit-client|@livekit/[\w-]+)\"\s*:",
     "LiveKit packages are not allowed; copy the visual component source in instead (D-5)"),
    ("provider-browser", r"\b(?:webkitSpeechRecognition|SpeechRecognition|speechSynthesis|SpeechSynthesisUtterance)\b",
     "browser Web Speech API bypasses Prisma/Timbre"),
]

# §0.2 — LLM endpoints come from config, never hardcoded in code.
LLM_HOST_RULES: list[tuple[str, str, str]] = [
    ("llm-hardcoded", r"api\.openai\.com|api\.anthropic\.com|generativelanguage\.googleapis\.com|"
     r"api\.groq\.com|api\.together\.xyz|openrouter\.ai|api\.mistral\.ai|api\.fireworks\.ai",
     "LLM host hardcoded; read LLM_BASE_URL / LLM_FALLBACK_BASE_URL from config"),
]

# §0.4 — fictional brands only. Case-insensitive, whole words. Applied to ALL text files.
REAL_BRANDS = [
    "airtel", "reliance jio", "jio", "vodafone", "vodafone idea", "bsnl", "mtnl", "tata play",
    "state bank of india", "sbi", "hdfc", "icici", "axis bank", "kotak", "yes bank", "punjab national bank",
    "bank of baroda", "canara bank", "union bank", "idfc", "indusind", "paytm", "phonepe", "google pay", "gpay",
    "bescom", "msedcl", "mahadiscom", "tata power", "adani electricity", "bses", "tneb", "tangedco", "kseb",
    "cesc", "torrent power", "flipkart", "swiggy", "zomato", "irctc", "makemytrip", "ola cabs", "uber",
    "amazon pay", "bajaj finserv", "lic of india",
]

# §0.6 — secrets. Applied to ALL text files.
SECRET_RULES: list[tuple[str, str, str]] = [
    ("secret-gnani", r"vach_[A-Za-z0-9_]{24,}", "Gnani API key literal"),
    ("secret-openai", r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_\-]{24,}", "API key literal"),
    ("secret-aws", r"\bAKIA[0-9A-Z]{16}\b", "AWS access key"),
    ("secret-github", r"\bgh[pousr]_[A-Za-z0-9]{30,}\b", "GitHub token"),
    ("secret-pem", r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", "private key"),
    ("secret-assign", r"(?i)\b(?:api[_-]?key|secret|token|password)\b\s*[:=]\s*['\"][A-Za-z0-9_\-/+]{20,}['\"]",
     "credential assigned in source"),
]

# §0.3 — synthetic data only. Mobile-like numbers must be declared in scripts/synthetic_allowlist.txt.
MOBILE_RE = r"(?<![\d.\w])(?:\+91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?![\d\w])"
AADHAAR_CTX_RE = r"(?i)(?:aadha+r|\buid\b)[^\n]{0,40}?\b[2-9]\d{3}[\s-]?\d{4}[\s-]?\d{4}\b"
PAN_RE = r"\b[A-Z]{3}[ABCFGHLJPT][A-Z]\d{4}[A-Z]\b"
