"""Preview/apply free-time settings. Stop the service before --apply, then restart it."""

import argparse
import copy
import json
from datetime import datetime, timezone
from pathlib import Path

from roundtable.settings import normalize_settings, resolve_data_dir


def updated(settings: dict) -> dict:
    result = copy.deepcopy(settings)
    enabled = [p for p in result.get("participants", []) if p.get("enabled", True)]
    for member in enabled:
        member.update(sleep="", busy="", busy_doing="")
    life = result.setdefault("life", {})
    members = [p["id"] for p in enabled]
    life.update(enabled=len(members) >= 2, members=members, hours="0-24",
                shares_per_day=3, quiet_minutes=10)
    life.setdefault("member_days", {}).update({pid: 0 for pid in members})
    passive = life.setdefault("passive_member_days", {})
    for pid in members:
        passive[pid] = 0 if passive.get(pid) == 0 else max(8, passive.get(pid, 4))
    normalize_settings(result)  # validate without rewriting unrelated fields
    return result


def apply(data_dir: Path) -> Path | None:
    path = data_dir / "settings.json"
    original = path.read_bytes()
    current = json.loads(original.decode("utf-8"))
    result = updated(current)
    if result == current:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = path.with_name(f"settings.before-free-life-{stamp}.json")
    with backup.open("xb") as stream:
        stream.write(original)
    temp = path.with_name("settings.free-life.tmp")
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)
    return backup


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    folder = resolve_data_dir(args.data_dir)
    if args.apply:
        print(apply(folder) or "Already applied.")
    else:
        result = updated(json.loads((folder / "settings.json").read_text(encoding="utf-8")))
        print(json.dumps({"life": result["life"], "routines": [{k: p.get(k) for k in ("id", "sleep", "busy")}
                         for p in result["participants"] if p.get("enabled", True)]}, ensure_ascii=False, indent=2))
