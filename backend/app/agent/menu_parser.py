"""IVR menu parsing, rules first (CLAUDE.md §9.2). Contract: backend/tests/contract/test_menu_parser.py.

Pipeline for one transcript segment:
  1. normalize: Devanagari key phrases mapped to Latin, the rest transliterated; lowercase; punctuation
     stripped; star/hash -> * #; number words -> digits. English number words convert everywhere;
     Hindi ones too, except "do"/"oh", which convert only next to a press verb ("to do a recharge" stays).
  2. find key mentions: "press|dial X" (key-first English) and "X dabaiye" / "X press karein" (Hindi).
  3. assign labels: Hindi mentions claim "<label> ke liye X dabaiye" (or "X dabaiye <label>");
     English mentions share the text between keys, given to the following key when the menu reads
     label-first ("for refund press 2 ...") and to the preceding key otherwise ("press 2 for refund").
  4. detect data prompts (enter/type/darj karein + mobile/account; PIN/OTP/CVV are "other": never send),
     voice prompts (say/tell us/boliye + query/problem/reason), and repeat hints.
needs_llm flags a segment that mentions keys but yielded no options; the LLM fallback (T-007) uses it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from indic_transliteration import sanscript

from app.config import get_settings

# ---------------- normalization ----------------

_DEVANAGARI_MAP = {
    "दबाइए": "dabaiye", "दबाइये": "dabaiye", "दबाईए": "dabaiye", "दबाएं": "dabaiye", "दबाएँ": "dabaiye",
    "दबायें": "dabaiye", "दबाये": "dabaiye", "दबाए": "dabaiye",
    "एक": "ek", "दो": "do", "तीन": "teen", "चार": "chaar", "पांच": "paanch", "पाँच": "paanch", "छह": "chhe",
    "छः": "chhe", "सात": "saat", "आठ": "aath", "नौ": "nau", "शून्य": "shunya", "ज़ीरो": "zero", "जीरो": "zero",
    "के": "ke", "लिए": "liye", "लिये": "liye", "हैश": "hash", "स्टार": "star",
    "दोबारा": "dobara", "सुनने": "sunne", "मोबाइल": "mobile", "नंबर": "number", "नम्बर": "number",
    "दर्ज": "darj", "करें": "karein", "खाता": "khaata", "संख्या": "sankhya", "अंकों": "ankon", "कृपया": "kripya",
    "बोलिए": "boliye", "बोलें": "boliye", "बताएं": "batayein", "बताइए": "bataiye", "समस्या": "samasya",
    "और": "aur", "का": "ka", "अपना": "apna", "अपनी": "apni",
}
_DEV_RE = re.compile(r"[ऀ-ॿ]+")

_EN_NUM = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
           "seven": "7", "eight": "8", "nine": "9"}
_HI_NUM = {"shunya": "0", "ek": "1", "teen": "3", "char": "4", "chaar": "4", "paanch": "5", "panch": "5",
           "chhe": "6", "chah": "6", "chhah": "6", "che": "6", "saat": "7", "aath": "8", "nau": "9"}
_CONTEXT_NUM = {"do": "2", "oh": "0"}  # converted only next to a press verb
_SYMBOLS = {"star": "*", "asterisk": "*", "hash": "#", "pound": "#"}

_VERB_AFTER = {"dabaiye", "dabayen", "dabaye", "dabaie", "dabaen", "dabayein", "dabaaiye", "dabaiyega", "dabaein"}
_PRESS_BEFORE = {"press", "dial"}
_PRESS_KAREIN = {"karein", "kare", "karen", "kijiye", "kariye"}
_KEY_RE = re.compile(r"^[0-9*#]$")


def _devanagari_to_latin(text: str) -> str:
    def word(m: re.Match[str]) -> str:
        w = m.group(0)
        if w in _DEVANAGARI_MAP:
            return _DEVANAGARI_MAP[w]
        return sanscript.transliterate(w, sanscript.DEVANAGARI, sanscript.ITRANS).lower()

    return _DEV_RE.sub(word, text)


def _tokens(text: str) -> list[str]:
    text = _devanagari_to_latin(text).lower()
    text = re.sub(r"[^\w*#\s]", " ", text)  # strip punctuation, keep * and #
    toks = text.split()
    out: list[str] = []
    for i, tok in enumerate(toks):
        prev = toks[i - 1] if i else ""
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        if tok in _EN_NUM:
            out.append(_EN_NUM[tok])
        elif tok in _HI_NUM:
            out.append(_HI_NUM[tok])
        elif tok in _CONTEXT_NUM and (prev in _PRESS_BEFORE or prev == "liye" or nxt in _VERB_AFTER
                                      or (nxt == "press" and i + 2 < len(toks) and toks[i + 2] in _PRESS_KAREIN)):
            out.append(_CONTEXT_NUM[tok])
        elif tok in _SYMBOLS:
            out.append(_SYMBOLS[tok])
        else:
            out.append(tok)
    return out


def normalize(text: str) -> str:
    return " ".join(_tokens(text))


# ---------------- parse result types ----------------


@dataclass(frozen=True)
class MenuOption:
    digit: str
    label: str
    segment_id: str


@dataclass(frozen=True)
class DataPrompt:
    field: str  # registered_mobile | account_id | other (sensitive/unknown: never send profile data)
    terminator: str | None


@dataclass
class SegmentParse:
    segment_id: str
    text: str
    normalized: str
    options: list[MenuOption] = field(default_factory=list)
    data_prompt: DataPrompt | None = None
    voice_prompt: bool = False
    repeat_hint: bool = False
    needs_llm: bool = False


# ---------------- detection rules ----------------

_REPEAT_RE = re.compile(r"\bto repeat\b|\bdobara sunne\b|\brepeat (?:this|these|the)\b")
_DATA_VERB_RE = re.compile(r"\b(?:enter|type|key in|darj karein|darj kare|dalein|daalein)\b")
_SENSITIVE_RE = re.compile(r"\b(?:pin|mpin|otp|cvv|password|one time password)\b")
_MOBILE_RE = re.compile(r"\b(?:mobile|phone)\b")
_ACCOUNT_RE = re.compile(r"\b(?:account|khaata|khata|customer id|customer number|consumer number)\b")
_VOICE_VERB_RE = re.compile(r"\b(?:tell us|say|speak|boliye|bolein|batayein|bataiye|bataye)\b")
_VOICE_OBJ_RE = re.compile(r"\b(?:query|problem|reason|samasya|issue|concern)\b")
_LLM_HINT_RE = re.compile(r"\b(?:press|dabaiye|dabayen|option|select|dial)\b")
_LEAD_CUES = {"for", "to", "if", "agar"}
_STRIP_LEAD = {"for", "to", "if", "agar", "aur", "or", "and", "then", "please", "kripya"}
_STRIP_TAIL = {"ke", "liye", "to", "then", "please", "kripya"}
_WELCOME_RE = re.compile(r"^.*\bswagat hai\b|^.*\bwelcome to [\w ]+?(?= for | to |$)")


def _data_prompt(norm: str) -> DataPrompt | None:
    if not _DATA_VERB_RE.search(norm):
        return None
    if _SENSITIVE_RE.search(norm):
        fld = "other"
    elif _MOBILE_RE.search(norm):
        fld = "registered_mobile"
    elif _ACCOUNT_RE.search(norm):
        fld = "account_id"
    elif re.search(r"\bnumber\b", norm):
        fld = "other"
    else:
        return None
    return DataPrompt(fld, "#" if "#" in norm.split() else None)


@dataclass
class _Mention:
    key: str
    start: int  # first token of the mention phrase
    end: int  # one past the last token
    hindi: bool  # "X dabaiye" form (label usually precedes, via "ke liye")


def _find_mentions(toks: list[str]) -> list[_Mention]:
    out: list[_Mention] = []
    i = 0
    while i < len(toks):
        tok = toks[i]
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        nxt2 = toks[i + 2] if i + 2 < len(toks) else ""
        if tok in _PRESS_BEFORE and _KEY_RE.match(nxt) and nxt2 not in _PRESS_KAREIN:
            out.append(_Mention(nxt, i, i + 2, hindi=False))
            i += 2
            continue
        if _KEY_RE.match(tok) and nxt in _VERB_AFTER:
            out.append(_Mention(tok, i, i + 2, hindi=True))
            i += 2
            continue
        if _KEY_RE.match(tok) and nxt == "press" and nxt2 in _PRESS_KAREIN:
            out.append(_Mention(tok, i, i + 3, hindi=True))
            i += 3
            continue
        i += 1
    return out


def _clean_label(words: list[str]) -> str:
    text = " ".join(words)
    text = _WELCOME_RE.sub("", text).strip()
    w = text.split()
    while w and w[0] in _STRIP_LEAD:
        w.pop(0)
    while w and w[-1] in _STRIP_TAIL:
        w.pop()
    return " ".join(w)


def _from_first_cue(words: list[str]) -> list[str]:
    for i, wd in enumerate(words):
        if wd in _LEAD_CUES:
            return words[i:]
    return words


def _assign_labels(toks: list[str], ms: list[_Mention]) -> dict[int, list[str]]:
    """Return mention index -> label words."""
    n = len(ms)
    labels: dict[int, list[str]] = {}
    claimed = [False] * len(toks)
    for m in ms:
        for k in range(m.start, m.end):
            claimed[k] = True

    def gap(a: int, b: int) -> list[int]:
        return [k for k in range(a, b) if not claimed[k]]

    bounds_before = [ms[i - 1].end if i else 0 for i in range(n)]
    bounds_after = [ms[i + 1].start if i + 1 < n else len(toks) for i in range(n)]

    # Hindi-form mentions claim their label first.
    for i, m in enumerate(ms):
        if not m.hindi:
            continue
        before = gap(bounds_before[i], m.start)
        bw = [toks[k] for k in before]
        if bw and (bw[-1] == "liye" or bw[-1] == "to" or bw[-2:] == ["ke", "lie"]):
            idx = before
        else:
            idx = gap(m.end, bounds_after[i])
        for k in idx:
            claimed[k] = True
        labels[i] = [toks[k] for k in idx]

    eng = [i for i, m in enumerate(ms) if not m.hindi]
    if eng:
        leading = gap(bounds_before[eng[0]], ms[eng[0]].start)
        trailing = gap(ms[eng[-1]].end, bounds_after[eng[-1]])
        label_first = bool(leading) and not trailing
        for i in eng:
            m = ms[i]
            if label_first:
                idx = gap(bounds_before[i], m.start)
                words = _from_first_cue([toks[k] for k in idx])
            else:
                idx = gap(m.end, bounds_after[i])
                words = [toks[k] for k in idx]
            for k in idx:
                claimed[k] = True
            labels[i] = words
    return labels


def parse_segment(text: str, segment_id: str = "s0") -> SegmentParse:
    toks = _tokens(text)
    norm = " ".join(toks)
    res = SegmentParse(segment_id=segment_id, text=text, normalized=norm)
    res.data_prompt = _data_prompt(norm)
    res.voice_prompt = bool(_VOICE_VERB_RE.search(norm) and _VOICE_OBJ_RE.search(norm))
    res.repeat_hint = bool(_REPEAT_RE.search(norm))

    mentions = _find_mentions(toks)
    if res.data_prompt is not None:  # "followed by hash" / "hash dabaiye" are terminators, not options
        mentions = [m for m in mentions if m.key not in "*#"]
    labels = _assign_labels(toks, mentions)
    seen: set[str] = set()
    for i, m in enumerate(mentions):
        if m.key in seen:
            continue
        seen.add(m.key)
        res.options.append(MenuOption(m.key, _clean_label(labels.get(i, [])), segment_id))
    res.needs_llm = (not res.options and res.data_prompt is None and not res.voice_prompt
                     and bool(_LLM_HINT_RE.search(norm)))
    return res


class MenuBuffer:
    """Accumulates options across segments until the menu is complete (§9.2)."""

    def __init__(self, complete_silence_s: float | None = None) -> None:
        self.complete_silence_s = (complete_silence_s if complete_silence_s is not None
                                   else get_settings().menu_complete_silence_s)
        self.options: list[MenuOption] = []
        self._last_t: float | None = None
        self._first_repeated = False
        self._repeat_hint = False
        self.segments: list[SegmentParse] = []

    def add(self, parse: SegmentParse, t_end: float) -> None:
        self.segments.append(parse)
        if parse.repeat_hint and (self.options or parse.options):
            self._repeat_hint = True
        have = {o.digit for o in self.options}
        for o in parse.options:
            if o.digit in have:
                if self.options and o.digit == self.options[0].digit:
                    self._first_repeated = True
                continue
            self.options.append(o)
            have.add(o.digit)
        if self.options:
            self._last_t = t_end

    def is_complete(self, now: float) -> bool:
        if not self.options:
            return False
        if self._first_repeated or self._repeat_hint:
            return True
        return self._last_t is not None and now - self._last_t >= self.complete_silence_s

    def reset(self) -> None:
        self.__init__(self.complete_silence_s)  # type: ignore[misc]
