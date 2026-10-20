"""Local inverse text normalisation for post-call (CLAUDE.md §12.3).

M0 showed Prisma's `format=transcribe` keeps number words ("press one"), so IDs and numbers are compacted
here, deterministically and for free:
  "n m b six six four four eight one six two"  -> "NMB66448162"
  "nine zero zero zero zero zero zero zero zero one" -> "9000000001"
  "double five" -> "55", "rupees 239" / "239 rupees" -> "₹239"
Runs of fewer than 3 symbols are left alone, so ordinary words ("a", "one of them") survive.
"""

from __future__ import annotations

import re

DIGIT_WORDS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
               "seven": "7", "eight": "8", "nine": "9"}
_MULT = {"double": 2, "triple": 3}
_RUPEE_RE = re.compile(r"(?:rupees|rupaye|rs\.?|inr)\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:rupees|rupaye|rs\b)", re.I)


def _symbol(tok: str) -> str | None:
    low = tok.lower()
    if low in DIGIT_WORDS:
        return DIGIT_WORDS[low]
    if re.fullmatch(r"\d+", tok):  # Prisma sometimes merges digits already ("56363973")
        return tok
    if re.fullmatch(r"[a-zA-Z]", tok):
        return tok.upper()
    return None


def compact_ids(text: str) -> str:
    toks = text.split()
    out: list[str] = []
    i = 0
    while i < len(toks):
        run: list[str] = []
        j = i
        while j < len(toks):
            low = toks[j].lower().strip(",.")
            nxt = _symbol(toks[j + 1].strip(",.")) if j + 1 < len(toks) else None
            if low in _MULT and nxt is not None:  # "double five" -> "55"
                run.append(nxt * _MULT[low])
                j += 2
                continue
            sym = _symbol(toks[j].strip(",."))
            if sym is None:
                break
            run.append(sym)
            j += 1
        has_digit = any(c.isdigit() for c in "".join(run))
        if len(run) >= 3 and has_digit:
            out.append("".join(run))
            i = j
        else:
            out.append(toks[i])
            i += 1
    return " ".join(out)


_PHONETIC_RE = re.compile(r"\b([a-z])\s+(?:for|as in)\s+([a-z]+)\b", re.I)
_BLOCK_DIGITS_RE = re.compile(r"\b([b-df-hj-np-tv-z]{2,4})\s?(\d{6,})\b", re.I)


def phonetic_letters(text: str) -> str:
    """'n for nashik, m for mumbai, b for bangalore' -> 'N M B' (the word decides the letter)."""
    return _PHONETIC_RE.sub(lambda m: m.group(2)[0].upper(), text).replace(",", " ")


def itn(text: str) -> str:
    t = compact_ids(phonetic_letters(text))
    t = _BLOCK_DIGITS_RE.sub(lambda m: m.group(1).upper() + m.group(2), t)
    return _RUPEE_RE.sub(lambda m: "₹" + (m.group(1) or m.group(2)), t)


def has_numbers(text: str) -> bool:
    low = text.lower()
    return bool(re.search(r"\d", low)) or sum(w in DIGIT_WORDS for w in low.split()) >= 2
