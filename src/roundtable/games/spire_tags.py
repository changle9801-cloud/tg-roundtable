"""Which Slay the Spire cards belong to which build, by the game's own card ids.

Only the Ironclad for now (the only class the members play). A card missing here just
doesn't count towards a build.
"""

from __future__ import annotations

# build -> core cards (the build hinges on one of them) and support cards (it gets better with them)
IRONCLAD_TAGS: dict[str, dict[str, list[str]]] = {
    "力量": {"core": ["Inflame", "Spot Weakness", "Limit Break", "Demon Form"],
             "support": ["Heavy Blade", "Sword Boomerang", "Twin Strike", "Pummel", "Whirlwind"]},
    "消耗": {"core": ["Corruption", "Feel No Pain", "Dark Embrace"],
             "support": ["Second Wind", "Burning Pact", "True Grit", "Sever Soul", "Fiend Fire"]},
    "格挡": {"core": ["Barricade", "Body Slam", "Juggernaut"],
             "support": ["Entrench", "Impervious", "Flame Barrier", "Shrug It Off", "Power Through"]},
    "自伤": {"core": ["Rupture"],
             "support": ["Bloodletting", "Offering", "Hemokinesis", "Combust"]},
}

TAGS = {"IRONCLAD": IRONCLAD_TAGS}


def card_builds(card_id: str, cls: str = "IRONCLAD") -> list[tuple[str, str]]:
    """[(build, "core" | "support")] for one card id."""
    out = []
    for build, parts in TAGS.get(cls, {}).items():
        for role, cards in parts.items():
            if card_id in cards:
                out.append((build, role))
    return out
