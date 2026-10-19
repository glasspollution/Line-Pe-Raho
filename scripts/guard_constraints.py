"""Static guard for CLAUDE.md §0 hard constraints. Stdlib only; runs in <1 s.

Errors (exit 1): non-Gnani speech providers, hardcoded LLM hosts, real brand names, secrets,
.env tracked or not ignored, undeclared mobile-like numbers.
Warnings (exit 0): Aadhaar/PAN-shaped strings.

Suppress a single line with a trailing comment:  guard: allow <rule-id> -- <reason>

Usage: python scripts/guard_constraints.py [--json] [--root PATH]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import guard_rules as R  # noqa: E402

ALLOW_RE = re.compile(r"guard:\s*allow\s+([\w-]+)")


@dataclass
class Finding:
    level: str  # "error" | "warning"
    rule: str
    path: str
    line: int
    message: str
    excerpt: str


def list_files(root: Path) -> list[Path]:
    """Tracked + untracked-not-ignored files when in git, else a filtered walk."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=root, capture_output=True, check=True,
        ).stdout.decode("utf-8", "replace")
        rels = [p for p in out.split("\0") if p]
        files = [root / p for p in rels]
    except (OSError, subprocess.CalledProcessError):
        files = [p for p in root.rglob("*") if p.is_file()]
    keep = []
    for f in files:
        rel = f.relative_to(root)
        if any(part in R.SKIP_DIRS for part in rel.parts[:-1]):
            continue
        if rel.as_posix() in R.SKIP_FILES:
            continue
        if f.suffix.lower() in R.TEXT_EXT or f.name.startswith(".env"):
            keep.append(f)
    return keep


def load_allowlist(root: Path) -> set[str]:
    path = root / "scripts" / "synthetic_allowlist.txt"
    if not path.exists():
        return set()
    vals = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            vals.add(re.sub(r"\D", "", line)[-10:])
    return vals


def _compile(rules: list[tuple[str, str, str]]) -> list[tuple[str, re.Pattern[str], str]]:
    return [(rid, re.compile(rx, re.MULTILINE), msg) for rid, rx, msg in rules]


SPEECH = _compile(R.SPEECH_PROVIDER_RULES)
LLM = _compile(R.LLM_HOST_RULES)
SECRETS = _compile(R.SECRET_RULES)
BRANDS = re.compile(r"(?i)(?<![\w-])(?:" + "|".join(re.escape(b) for b in R.REAL_BRANDS) + r")(?![\w-])")
MOBILE = re.compile(R.MOBILE_RE)
AADHAAR = re.compile(R.AADHAAR_CTX_RE)
PAN = re.compile(R.PAN_RE)


def scan_text(rel: str, text: str, is_code: bool, allow: set[str]) -> list[Finding]:
    found: list[Finding] = []
    for i, line in enumerate(text.splitlines(), 1):
        suppressed = set(ALLOW_RE.findall(line))
        hits: list[tuple[str, str, str]] = []  # (level, rule, message)
        if is_code:
            hits += [("error", rid, msg) for rid, rx, msg in SPEECH + LLM if rx.search(line)]
        hits += [("error", rid, msg) for rid, rx, msg in SECRETS if rx.search(line)]
        if (m := BRANDS.search(line)):
            hits.append(("error", "real-brand", f"real company name '{m.group(0)}' (use fictional brands)"))
        for m in MOBILE.finditer(line):
            if re.sub(r"\D", "", m.group(0))[-10:] not in allow:
                hits.append(("error", "pii-mobile",
                             f"mobile-like number {m.group(0)!r} not declared in scripts/synthetic_allowlist.txt"))
        if AADHAAR.search(line):
            hits.append(("warning", "pii-aadhaar", "Aadhaar-shaped number"))
        if PAN.search(line):
            hits.append(("warning", "pii-pan", "PAN-shaped string"))
        found += [Finding(lvl, rule, rel, i, msg, line.strip()[:160]) for lvl, rule, msg in hits
                  if rule not in suppressed]
    return found


def check_env_hygiene(root: Path) -> list[Finding]:
    found: list[Finding] = []
    try:
        tracked = subprocess.run(["git", "ls-files", "--cached", "--", ".env"], cwd=root,
                                 capture_output=True, text=True, check=True).stdout.strip()
        if tracked:
            found.append(Finding("error", "env-tracked", ".env", 0, ".env is tracked by git", ""))
        if (root / ".env").exists():
            ignored = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=root).returncode == 0
            if not ignored:
                found.append(Finding("error", "env-not-ignored", ".gitignore", 0, ".env is not gitignored", ""))
    except (OSError, subprocess.CalledProcessError):
        pass
    return found


def run(root: Path) -> list[Finding]:
    allow = load_allowlist(root)
    findings = check_env_hygiene(root)
    for f in list_files(root):
        rel = f.relative_to(root).as_posix()
        if f.name.startswith(".env") and f.name != ".env.example":
            continue  # real secrets live here by design; hygiene checked above
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        findings += scan_text(rel, text, f.suffix.lower() in R.CODE_EXT, allow)
    return findings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    args = ap.parse_args()
    findings = run(Path(args.root))
    errors = [f for f in findings if f.level == "error"]
    if args.json:
        print(json.dumps([asdict(f) for f in findings], indent=2))
    else:
        for f in findings:
            print(f"{f.level.upper():7} {f.rule:16} {f.path}:{f.line}  {f.message}\n          {f.excerpt}")
        print(f"guard: {len(errors)} error(s), {len(findings) - len(errors)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
