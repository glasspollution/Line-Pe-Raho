"""Fixed agent phrases (CLAUDE.md §0.5, §10.3). Contract: test_phrases.py.

- The disclosure and the liveness probe both say the agent is an AI (D-1), in the opening words.
- Verb gender follows the configured agent voice (Shivani -> female "kar rahi hoon", Avinash -> male).
- The user's name is sanitised before it reaches TTS (no audio tags, digits or symbols).
- All text is TTS-ready: no digits.
"""

from __future__ import annotations

import re
from typing import Literal

from app.config import get_settings

LANGS = ("en-IN", "hi-IN", "hi-en")
Gender = Literal["f", "m"]
VOICE_GENDER: dict[str, Gender] = {"shivani": "f", "riyaa": "f", "avinash": "m"}
_TAG_RE = re.compile(r"<[^>]*>")
_NAME_KEEP_RE = re.compile(r"[^\w\s'\-]|[\d_]")


def agent_gender() -> Gender:
    return VOICE_GENDER.get(get_settings().tts_agent_voice.lower(), "f")


def clean_name(name: str) -> str:
    n = _NAME_KEEP_RE.sub(" ", _TAG_RE.sub(" ", name))
    n = " ".join(n.split())[:40].strip()
    return n or "user"


def _g(gender: Gender | None, f: str, m: str) -> str:
    return f if (gender or agent_gender()) == "f" else m


def _check(lang: str) -> None:
    if lang not in LANGS:
        raise ValueError(f"unsupported language {lang!r}; expected one of {LANGS}")


def disclosure(lang: str, user_first_name: str, gender: Gender | None = None) -> str:
    _check(lang)
    n = clean_name(user_first_name)
    if lang == "en-IN":
        return (f"Hello, I am an AI assistant calling on behalf of {n}. "
                "I am connecting them to this call now, please hold for a moment.")
    if lang == "hi-en":
        v = _g(gender, "rahi", "raha")
        return (f"Namaste, main ek AI assistant hoon aur {n} ji ki taraf se call kar {v} hoon. "
                f"Main unhe abhi line par jod {v} hoon, kripya ek pal rukiye.")
    v = _g(gender, "रही", "रहा")
    return (f"नमस्ते, मैं एक एआई असिस्टेंट हूँ और {n} जी की तरफ़ से कॉल कर {v} हूँ। "
            f"मैं उन्हें अभी लाइन पर जोड़ {v} हूँ, कृपया एक पल रुकिए।")


def probe(lang: str, user_first_name: str, gender: Gender | None = None) -> str:
    """Liveness probe. It may reach a human, so it discloses AI itself (D-1)."""
    _check(lang)
    n = clean_name(user_first_name)
    if lang == "en-IN":
        return f"Hello? I am an AI assistant calling on behalf of {n}. Can you hear me?"
    if lang == "hi-en":
        v = _g(gender, "rahi", "raha")
        return f"Hello? Main ek AI assistant hoon, {n} ji ki taraf se call kar {v} hoon. Kya aap mujhe sun pa rahe hain?"
    v = _g(gender, "रही", "रहा")
    return f"हेलो? मैं एक एआई असिस्टेंट हूँ, {n} जी की तरफ़ से कॉल कर {v} हूँ। क्या आप मुझे सुन पा रहे हैं?"


def keepalives(lang: str) -> list[str]:
    _check(lang)
    if lang == "en-IN":
        return ["Just a second, they are joining the line now.",
                "Thank you for waiting, they are joining now.",
                "Please stay on the line, they will be with you shortly."]
    if lang == "hi-en":
        return ["Bas ek second, woh line par aa rahe hain.",
                "Thank you for waiting, they are joining now.",
                "Kripya line par bane rahiye."]
    return ["बस एक सेकंड, वो लाइन पर आ रहे हैं।",
            "रुकने के लिए धन्यवाद, वो अभी जुड़ रहे हैं।",
            "कृपया लाइन पर बने रहिए।"]


def no_join_fallback(lang: str) -> str:
    _check(lang)
    if lang == "en-IN":
        return ("They are not available right now. Could you please share the complaint number, "
                "so they can follow up with you later?")
    if lang == "hi-en":
        return ("Woh abhi uplabdh nahi hain. Kya aap mujhe complaint number bata sakte hain, "
                "taaki woh aapse baad mein sampark kar sakein?")
    return "वो अभी उपलब्ध नहीं हैं। क्या आप मुझे कंप्लेंट नंबर बता सकते हैं, ताकि वो आपसे बाद में संपर्क कर सकें?"


def fixed_phrases(lang: str) -> list[str]:
    """Name-independent phrases to pre-synthesise at startup (PhraseCache, §7.4)."""
    return [*keepalives(lang), no_join_fallback(lang)]


def per_user_phrases(lang: str, user_first_name: str) -> list[str]:
    """Pre-synthesised at task start so the disclosure plays with zero network latency."""
    return [disclosure(lang, user_first_name), probe(lang, user_first_name)]
