import importlib.util
import json
from pathlib import Path

from nookling.settings import normalize_settings


def script():
    spec = importlib.util.spec_from_file_location("apply_free_life", Path(__file__).parents[1] / "scripts/apply_free_life.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_free_life_settings_clear_routines_preserve_roles_and_back_up_exact_bytes(tmp_path):
    from test_life import MEMBERS
    settings = normalize_settings({"participants": MEMBERS, "life": {"enabled": True, "members": ["claude", "codex"],
                                  "member_days": {"claude": 0, "codex": 4}}})
    settings["participants"][0].update(sleep="23:30-07:30", busy="09:30-18:00", style="不要改变我的口吻")
    path = tmp_path / "settings.json"
    original = json.dumps(settings, ensure_ascii=False, indent=4).encode("utf-8")
    path.write_bytes(original)
    change = script()
    preview = change.updated(settings)
    assert path.read_bytes() == original
    assert preview["participants"][0]["sleep"] == preview["participants"][0]["busy"] == ""
    assert preview["participants"][0]["style"] == "不要改变我的口吻"
    assert preview["life"]["hours"] == "0-24"
    assert preview["life"]["member_days"] == {"claude": 0, "codex": 0}
    assert normalize_settings(preview)["life"]["shares_per_day"] == 3
    backup = change.apply(tmp_path)
    assert backup.read_bytes() == original
    assert change.apply(tmp_path) is None
