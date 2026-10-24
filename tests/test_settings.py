import pytest

from roundtable.config import ConfigError
from roundtable.settings import (
    SettingsStore,
    apply_secret_changes,
    build_config,
    import_legacy_toml,
    masked_secrets,
    normalize_settings,
    prune_secrets,
)

LEGACY = """
data_dir = "~/.tg-roundtable"
[telegram]
controller_token = "111:claude-token-aaaa"
allowed_user_ids = [42]
[round]
max_turns_per_model = 2
[[participants]]
id = "claude"
name = "Claude"
kind = "claude_cli"
bot_token = "111:claude-token-aaaa"
[[participants]]
id = "ds"
name = "DeepSeek"
kind = "api"
enabled = false
provider = "deepseek"
api_key = "sk-secret-1234"
bot_token = "222:ds-token-bbbb"
model = "deepseek-flash"
"""


def test_import_legacy_splits_secrets_out(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(LEGACY, encoding="utf-8")
    settings, secrets = import_legacy_toml(path)

    assert settings["telegram"]["controller_participant"] == "claude"
    assert settings["telegram"]["allowed_user_ids"] == [42]
    assert settings["round"]["max_turns_per_model"] == 2
    assert secrets["bots"] == {"claude": "111:claude-token-aaaa", "ds": "222:ds-token-bbbb"}
    assert secrets["api_keys"] == {"ds": "sk-secret-1234"}
    assert "token" not in str(settings) and "sk-secret" not in str(settings)

    cfg = build_config(settings, secrets, tmp_path)
    assert cfg.telegram.controller_token == "111:claude-token-aaaa"
    assert [p.id for p in cfg.enabled_participants] == ["claude"]
    ds = next(p for p in cfg.participants if p.id == "ds")
    assert ds.api_key == "sk-secret-1234" and ds.bot_token == "222:ds-token-bbbb"


def test_store_round_trip_keeps_files_separate(tmp_path):
    store = SettingsStore(tmp_path)
    store.save(
        {"participants": [{"id": "a", "name": "A", "kind": "claude_cli"}]},
        {"controller_token": "", "bots": {"a": "999:tok-zzzz"}, "api_keys": {}},
    )
    assert "999:tok" not in (tmp_path / "settings.json").read_text(encoding="utf-8")
    settings, secrets = store.load()
    assert settings["participants"][0]["id"] == "a"
    assert secrets["bots"]["a"] == "999:tok-zzzz"
    assert store.web_token() == store.web_token()  # generated once, then reused


@pytest.mark.parametrize("bad_id", ["", "Has Upper", "中文", "a" * 33, "-start"])
def test_rejects_bad_member_ids(bad_id):
    with pytest.raises(ConfigError):
        normalize_settings({"participants": [{"id": bad_id, "name": "X", "kind": "api"}]})


def test_rejects_duplicates_and_unknown_kind():
    with pytest.raises(ConfigError, match="重复"):
        normalize_settings({"participants": [{"id": "a", "name": "A", "kind": "api"}] * 2})
    with pytest.raises(ConfigError, match="不支持"):
        normalize_settings({"participants": [{"id": "a", "name": "A", "kind": "nope"}]})


def test_normalize_parses_text_inputs():
    s = normalize_settings({
        "telegram": {"allowed_user_ids": "1, 2，3", "allowed_chat_ids": "-100"},
        "participants": [{"id": "a", "name": "A", "kind": "api", "aliases": "x，y", "params": '{"temperature": 1}'}],
    })
    assert s["telegram"]["allowed_user_ids"] == [1, 2, 3]
    assert s["telegram"]["allowed_chat_ids"] == [-100]
    assert s["participants"][0]["aliases"] == ["x", "y"]
    assert s["participants"][0]["params"] == {"temperature": 1}


def test_controller_pointing_at_missing_member_is_dropped():
    s = normalize_settings({"telegram": {"controller_participant": "ghost"}})
    assert s["telegram"]["controller_participant"] == ""


def test_secret_changes_set_clear_and_reject_unknown():
    secrets = {"controller_token": "c", "bots": {"a": "old-token-1"}, "api_keys": {"a": "k"}}
    out = apply_secret_changes(secrets, {"bots": {"a": "new-token-2"}, "api_keys": {"a": None}}, {"a"})
    assert out["bots"]["a"] == "new-token-2"
    assert "a" not in out["api_keys"]
    assert secrets["bots"]["a"] == "old-token-1"  # input untouched
    with pytest.raises(ConfigError):
        apply_secret_changes(secrets, {"bots": {"ghost": "t"}}, {"a"})


def test_prune_and_mask():
    secrets = {"controller_token": "123456789:ABCDEFGH", "bots": {"a": "t1-xxxxxxxx", "gone": "t2"}, "api_keys": {}}
    assert prune_secrets(secrets, {"a"})["bots"] == {"a": "t1-xxxxxxxx"}
    masked = masked_secrets(secrets)
    assert masked["controller_token"] == "…EFGH"
    assert "123456789" not in str(masked)
