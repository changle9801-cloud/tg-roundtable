from nookling import owner
from nookling.home import Home
from nookling.settings import SettingsStore, normalize_settings
from nookling.transcript import USER, Message, guest_note


def test_the_owner_is_called_what_the_settings_say(tmp_path):
    from nookling.runtime import Runtime
    try:
        store = SettingsStore(tmp_path)
        store.save({"participants": [{"id": "a", "name": "A", "kind": "claude_cli"}],
                    "prompts": {"owner_name": "  阿杰  "}})
        r = Runtime(store)
        assert owner.name() == "阿杰"
        assert "阿杰能翻你的日记本" in r.system_prompt_for(r._member("a").cfg)
        assert "阿杰拉进群的朋友" in guest_note([Message(USER, "小林", "嗨", (), 1.0, guest=True)])
        home = Home(tmp_path)
        home.ensure({"a": "A"})
        home.read_diary("a", "2026-10-29")
        assert home.tell("a").startswith("[日记本] 阿杰翻了你")
    finally:
        owner.set_name(None)


def test_a_missing_or_blank_name_falls_back_to_the_default():
    assert normalize_settings({})["prompts"]["owner_name"] == owner.DEFAULT
    assert normalize_settings({"prompts": {"owner_name": "   "}})["prompts"]["owner_name"] == owner.DEFAULT
    assert len(normalize_settings({"prompts": {"owner_name": "很长" * 20}})["prompts"]["owner_name"]) == owner.MAX_LENGTH
