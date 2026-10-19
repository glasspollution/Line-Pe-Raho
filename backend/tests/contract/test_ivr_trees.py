"""Contract: mock company IVR trees (CLAUDE.md §11.1). Task T-105.

Static checks on backend/app/company/trees/*.yaml — no engine needed. Node kinds:
  say+options (menu) | collect+next | ask+routes (voice prompt) | hold+then | say+next | agent
Special targets: "agent" (human console / auto-agent), "hangup". on_timeout: repeat | goto:<node> | hangup.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
import yaml

from tests.contract.helpers import ROOT

pytestmark = pytest.mark.milestone("M1")

TREES = ROOT / "backend" / "app" / "company" / "trees"
REQUIRED = {"nimbus_telecom", "sahyog_bank", "prakash_bijli_board"}
SPECIAL = {"agent", "hangup"}
PROFILE_FIELDS = {"registered_mobile", "account_id"}


@pytest.fixture(scope="module")
def trees() -> dict[str, dict[str, Any]]:
    files = sorted(TREES.glob("*.yaml")) if TREES.exists() else []
    if not files:
        pytest.skip("NOT IMPLEMENTED: app.company.trees [T-105]")
    return {f.stem: yaml.safe_load(f.read_text(encoding="utf-8")) for f in files}


def edges(node: dict[str, Any]) -> list[str]:
    out: list[str] = list((node.get("options") or {}).values())
    for key in ("next", "then"):
        if node.get(key):
            out.append(node[key])
    ask = node.get("ask") or {}
    out += list((ask.get("routes") or {}).values())
    if ask.get("fallback"):
        out.append(ask["fallback"])
    tmo = str(node.get("on_timeout", ""))
    if tmo.startswith("goto:"):
        out.append(tmo[5:])
    return out


def menu_depth(tree: dict[str, Any]) -> int:
    """Longest simple path (counting menu/collect/ask nodes) from root to a hold/agent node."""
    nodes = tree["nodes"]
    best = 0

    def dfs(name: str, seen: frozenset[str], depth: int) -> None:
        nonlocal best
        if name in SPECIAL or name not in nodes:
            if name == "agent":
                best = max(best, depth)
            return
        node = nodes[name]
        d = depth + (1 if any(k in node for k in ("options", "collect", "ask")) else 0)
        if "hold" in node:
            best = max(best, d)
        for nxt in edges(node):
            if nxt not in seen:
                dfs(nxt, seen | {nxt}, d)

    dfs(tree["root"], frozenset({tree["root"]}), 0)
    return best


def all_text(tree: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    for node in tree["nodes"].values():
        texts += [node.get("say", "")]
        texts += [(node.get("collect") or {}).get("prompt", ""), (node.get("ask") or {}).get("prompt", "")]
        texts += list((node.get("hold") or {}).get("announcements", []))
    return [t for t in texts if t]


def test_required_companies_present(trees: dict[str, dict[str, Any]]) -> None:
    assert REQUIRED <= set(trees)


def test_structure_and_references(trees: dict[str, dict[str, Any]]) -> None:
    for stem, t in trees.items():
        assert t["id"] == stem
        assert t.get("display_name") and t.get("voice", {}).get("name")
        assert t["root"] in t["nodes"]
        for name, node in t["nodes"].items():
            for nxt in edges(node):
                assert nxt in t["nodes"] or nxt in SPECIAL, f"{stem}.{name} -> unknown {nxt}"
            for digit in (node.get("options") or {}):
                assert re.fullmatch(r"[0-9*#]", str(digit)), f"{stem}.{name} bad key {digit!r}"
            if "collect" in node:
                c = node["collect"]
                assert c["field"] in PROFILE_FIELDS, f"{stem}.{name} collects unknown field"
                assert c.get("terminator") in ("#", None)
                assert isinstance(c.get("digits"), int)


def test_all_nodes_reachable(trees: dict[str, dict[str, Any]]) -> None:
    for stem, t in trees.items():
        seen, stack = {t["root"]}, [t["root"]]
        while stack:
            for nxt in edges(t["nodes"][stack.pop()]):
                if nxt in t["nodes"] and nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        assert seen == set(t["nodes"]), f"{stem} unreachable: {set(t['nodes']) - seen}"


def test_every_tree_can_reach_a_human(trees: dict[str, dict[str, Any]]) -> None:
    for stem, t in trees.items():
        assert menu_depth(t) >= 1, f"{stem} has no path to hold/agent"


def test_depth_range_and_variety(trees: dict[str, dict[str, Any]]) -> None:
    depths = {s: menu_depth(trees[s]) for s in REQUIRED}
    assert all(3 <= d <= 6 for d in depths.values()), depths
    assert len(set(depths.values())) >= 2, f"depths should vary: {depths}"


def test_variety_requirements(trees: dict[str, dict[str, Any]]) -> None:
    req = [trees[s] for s in REQUIRED]
    assert any(any("ask" in n for n in t["nodes"].values()) for t in req), "need a voice-prompt (ask) node"
    connecting = [any(re.search(r"connect(ing)? you|jod rahe|transfer", x, re.I) for x in all_text(t)) for t in req]
    assert any(connecting) and not all(connecting), "need 'connecting you to an agent' in some but not all"
    hello = [a for t in req for n in t["nodes"].values() for a in (n.get("hold") or {}).get("announcements", [])
             if a.strip().lower().startswith("hello")]
    assert hello, "need a hold announcement that begins with 'Hello' (adversarial)"
    first_lang = []
    for t in req:
        say = t["nodes"][t["root"]].get("say", "").lower()
        e, h = say.find("english"), say.find("hindi")
        first_lang.append("en" if 0 <= e < h or h < 0 else "hi")
    assert len(set(first_lang)) == 2, f"language order should vary across companies: {first_lang}"
