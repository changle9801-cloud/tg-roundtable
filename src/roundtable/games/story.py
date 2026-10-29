"""What happened in a Slay the Spire climb, read straight from the recorded actions.

Everything here is computed from ``actions.jsonl`` events (see ``GameService``): the build
the member was going for and whether it came together, the card it kept wanting, what
killed it, close calls and big plays. Models only put feelings and words to these facts.
"""

from __future__ import annotations

import re
from collections import Counter

from .spire_tags import TAGS

CLOSE_CALL = 0.15  # won a fight with less than this share of max HP left
BIG_HIT = 0.30  # one card took a monster from above this share of its HP to dead...
BIG_MONSTER = 20  # ...and it was not a tiny one
WANTED_MENTIONS = 3  # a card named this often in the reasons, never offered, never taken


def _game(side: dict) -> dict:
    return side.get("game", {}) if side.get("in_game", True) else {}


def _alive(monster: dict) -> bool:
    return not monster.get("is_gone") and not monster.get("half_dead") and (monster.get("current_hp") or 0) > 0


def _build_state(deck: list[dict], cls: str) -> dict[str, dict]:
    ids = Counter(c.get("id") for c in deck)
    out = {}
    for build, parts in TAGS.get(cls, {}).items():
        core = sum(ids[c] for c in parts["core"])
        support = sum(ids[c] for c in parts["support"])
        if core or support:
            names = sorted({c.get("name", c.get("id")) for c in deck if c.get("id") in parts["core"] + parts["support"]})
            out[build] = {"core": core, "support": support, "cards": names,
                          "formed": core >= 1 and support + core >= 3}
    return out


def _main_build(builds: dict[str, dict]) -> str | None:
    trying = {b: s for b, s in builds.items() if s["core"] >= 1 or s["core"] + s["support"] >= 3}
    if not trying:
        return None
    return max(trying, key=lambda b: (trying[b]["formed"], trying[b]["core"] * 2 + trying[b]["support"]))


def signals(events: list[dict], game_over: dict | None = None) -> dict:
    """The story of one climb (or of the part of it someone watched)."""
    events = [e for e in events if not e.get("error") and e.get("before", {}).get("in_game")]
    last: dict = {}
    for e in events:
        if _game(e.get("after", {})):
            last = _game(e["after"])
    cls = last.get("class") or "IRONCLAD"
    seen_names: set[str] = set()
    fights: list[dict] = []
    fight: dict | None = None
    offered: set[str] = set()
    skipped = 0
    key_plays: list[dict] = []
    formed_floor: dict[str, int] = {}
    died = False

    for e in events:
        before, after = _game(e["before"]), _game(e.get("after", {}))
        for side in (before, after):
            for c in side.get("deck", []) + side.get("combat_state", {}).get("hand", []):
                seen_names.add(c.get("name", c.get("id", "")))
            for m in side.get("combat_state", {}).get("monsters", []):
                seen_names.add(m.get("name", ""))
        if before.get("screen_type") == "CARD_REWARD":
            for c in before.get("screen_state", {}).get("cards", []):
                offered.add(c.get("id", ""))
                offered.add(c.get("name", ""))
                seen_names.add(c.get("name", ""))
            if after.get("screen_type") != "CARD_REWARD" and len(after.get("deck", [])) == len(before.get("deck", [])):
                skipped += 1
        if before.get("room_phase") == "COMBAT":
            monsters = [m.get("name", "") for m in before.get("combat_state", {}).get("monsters", [])]
            if fight is None or fight["floor"] != before.get("floor"):
                fight = {"floor": before.get("floor"), "act": before.get("act"), "monsters": sorted(set(monsters)),
                         "hp_before": before.get("current_hp"), "max_hp": before.get("max_hp"), "won": False}
                fights.append(fight)
            command = str(e.get("command", "")).split()
            if command and command[0] == "play" and len(command) > 1 and command[1].isdigit():
                hand = before.get("combat_state", {}).get("hand", [])
                index = int(command[1]) - 1
                old = before.get("combat_state", {}).get("monsters", [])
                new = after.get("combat_state", {}).get("monsters", [])
                if 0 <= index < len(hand) and len(old) == len(new):
                    for m0, m1 in zip(old, new):
                        big = (m0.get("max_hp") or 0) >= BIG_MONSTER
                        if big and _alive(m0) and (m0.get("current_hp") or 0) > BIG_HIT * m0["max_hp"] and not _alive(m1):
                            key_plays.append({"floor": before.get("floor"), "card": hand[index].get("name", ""),
                                              "monster": m0.get("name", "")})
            if after and after.get("room_phase") != "COMBAT":
                fight["hp_after"] = after.get("current_hp")
                fight["won"] = (after.get("current_hp") or 0) > 0 and after.get("screen_type") != "GAME_OVER"
        if after.get("screen_type") == "GAME_OVER" or (after and after.get("current_hp") == 0):
            died = True
        for build, state in _build_state(after.get("deck", []), cls).items():
            if state["formed"] and build not in formed_floor:
                formed_floor[build] = after.get("floor")

    builds = _build_state(last.get("deck", []), cls)
    main = _main_build(builds)
    victory = bool((game_over or {}).get("victory"))
    died = died and not victory
    death = fights[-1] if died and fights else None

    deck_names = {c.get("name", "") for c in last.get("deck", [])} | {c.get("id", "") for c in last.get("deck", [])}
    reasons = " ".join(str(e.get("reason", "")) for e in events).casefold()
    known = {c for parts in TAGS.get(cls, {}).values() for cards in parts.values() for c in cards} | offered
    wanted = sorted(n for n in known if n and n not in deck_names and n not in offered
                    and len(re.findall(re.escape(n.casefold()), reasons)) >= WANTED_MENTIONS)

    close_calls = [{"floor": f["floor"], "hp": f.get("hp_after"), "max_hp": f["max_hp"], "monsters": f["monsters"]}
                   for f in fights if f["won"] and f.get("hp_after") is not None
                   and f["hp_after"] < CLOSE_CALL * (f["max_hp"] or 1)]

    result = {
        "class": cls,
        "floor": last.get("floor"),
        "act": last.get("act"),
        "hp": last.get("current_hp"),
        "max_hp": last.get("max_hp"),
        "died": died,
        "won_game": victory,
        "killer": death["monsters"] if death else [],
        "death_floor": death["floor"] if death else None,
        "hp_before_death_fight": death["hp_before"] if death else None,
        "build": ({"name": main, "formed": builds[main]["formed"], "formed_floor": formed_floor.get(main),
                   "cards": builds[main]["cards"]} if main else None),
        "wanted": wanted,
        "close_calls": close_calls,
        "key_plays": key_plays[-3:],
        "skipped_rewards": skipped,
        "fights": [{k: f.get(k) for k in ("floor", "monsters", "won", "hp_before", "hp_after")} for f in fights],
    }
    facts: set[str] = set(result["killer"]) | set(wanted)
    if result["build"]:
        facts |= set(result["build"]["cards"])
    for c in close_calls:
        facts |= set(c["monsters"])
    for k in key_plays[-3:]:
        facts |= {k["card"], k["monster"]}
    result["fact_names"] = sorted(n for n in facts if n)
    result["names_seen"] = sorted(n for n in seen_names | known if n)
    numbers = {result["floor"], result["act"], result["hp"], result["max_hp"], result["death_floor"],
               result["hp_before_death_fight"], (result["build"] or {}).get("formed_floor")}
    for c in close_calls:
        numbers |= {c["floor"], c["hp"], c["max_hp"]}
    for k in key_plays[-3:]:
        numbers.add(k["floor"])
    result["numbers"] = sorted(n for n in numbers if isinstance(n, int))
    return result


def worth_remembering(sig: dict) -> bool:
    """A climb that ended on floor 2 with nothing going on leaves no memory."""
    return bool(sig["died"] or sig["won_game"] or sig["build"] or sig["wanted"] or sig["close_calls"]
                or sig["key_plays"] or (sig["floor"] or 0) >= 6)
