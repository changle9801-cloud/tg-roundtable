from nookling.apps import App
from nookling.apps.telegram import BOT_COMMANDS, TelegramApp
from nookling.runtime import Runtime
from nookling.settings import SettingsStore


def test_telegram_is_an_app_on_the_runtime(tmp_path):
    store = SettingsStore(tmp_path)
    store.save({"participants": [{"id": "a", "name": "A", "kind": "claude_cli"}]})
    r = Runtime(store)
    assert isinstance(r.telegram, TelegramApp)
    assert r.sink is r.telegram.sink
    assert not r.telegram.connected  # nothing is polling in tests
    assert r.status()["telegram"] == {"state": "未启动", "controller": "", "privacy_warning": False,
                                      "reaction_warning": False}
    assert {c.command for c in BOT_COMMANDS} >= {"home", "status", "stop"}
    for name in ("sync", "run", "status", "shutdown", "connected", "sink"):
        assert hasattr(r.telegram, name), name
    assert App  # the interface other chat apps implement
