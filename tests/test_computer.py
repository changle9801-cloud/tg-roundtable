import json

from nookling.home import Home
from nookling.settings import SettingsStore, build_config

MEMBERS = {"codex": "Codex"}


def test_old_telegram_settings_move_onto_the_computer_with_a_backup(tmp_path):
    old = {"telegram": {"allowed_user_ids": [111, 222], "allowed_chat_ids": [-100], "controller_participant": "a"},
           "participants": [{"id": "a", "name": "A", "kind": "claude_cli"}]}
    (tmp_path / "settings.json").write_text(json.dumps(old), encoding="utf-8")
    store = SettingsStore(tmp_path)
    settings, secrets = store.load()
    assert settings["apps"]["telegram"]["allowed_user_ids"] == [111, 222] and "telegram" not in settings
    saved = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert saved["apps"]["telegram"]["allowed_chat_ids"] == [-100] and "telegram" not in saved
    backup = json.loads((tmp_path / "settings.before-apps.json").read_text(encoding="utf-8"))
    assert backup["telegram"]["allowed_user_ids"] == [111, 222]  # the old file, as it was
    config = build_config(settings, {**secrets, "bots": {"a": "123:abc"}}, tmp_path)
    assert config.telegram.allowed_user_ids == [111, 222] and config.telegram.controller_token == "123:abc"
    store.load()  # nothing to convert the second time
    assert json.loads((tmp_path / "settings.before-apps.json").read_text(encoding="utf-8")) == backup


def test_a_new_house_has_the_computer_in_the_living_room(tmp_path):
    home = Home(tmp_path)
    home.ensure(MEMBERS)
    computer, = [p for p in home.pieces if p["type"] == "base/computer"]
    assert (computer["room"], computer["at"], computer["turn"], computer["functions"]) == ("living", [0, 6], 270, ["chat.telegram"])
    home.begin("codex", "share", "看到个好玩的")
    assert (home.where["codex"].room, home.where["codex"].piece) == ("living", computer["id"])


def test_an_older_house_gets_the_computer_once(tmp_path):
    home = Home(tmp_path)
    home.ensure(MEMBERS)
    home.pieces = [p for p in home.pieces if p["type"] != "base/computer"]  # a house from before it
    home.extras = []
    home.save()
    again = Home(tmp_path)
    again.ensure(MEMBERS)
    computer, = [p for p in again.pieces if p["type"] == "base/computer"]
    again.edit_piece(computer["id"], {"functions": []})
    again.remove(computer["id"])  # the owner took it out
    third = Home(tmp_path)
    third.ensure(MEMBERS)
    assert not [p for p in third.pieces if p["type"] == "base/computer"]  # not put back
