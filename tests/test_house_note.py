import random

from conftest import FakeSink

from nookling.conversation import Conversation
from nookling.home import MOVES_KEPT, Home
from nookling.moderator import RuleModerator

MEMBERS = {"codex": "Codex", "grok": "Grok"}


def house(tmp_path):
    home = Home(tmp_path)
    home.ensure(MEMBERS)
    return home


def test_she_is_told_the_rooms_what_they_hold_and_who_is_where(tmp_path):
    home = house(tmp_path)
    home.begin("grok", "watch", "随便看看")
    note = home.house_note("codex", MEMBERS)
    assert note.startswith("[家里现在]") and note.endswith("[/家里现在]")
    assert "一楼：" in note and "客厅" in note and "走廊" not in note  # hallways are not rooms to name
    assert "- 书房：" in note and "×3" in note  # the three computer desks, counted
    assert "电视（看视频、玩游戏）" in note  # what a piece is for
    assert "Codex的卧室：" in note and "Grok的卧室：" not in note  # her own bedroom's things, not another's
    assert "- Grok：娱乐室 · 在看视频" in note and "- 你：" in note
    assert "画框" not in note  # just to look at


def test_she_is_told_where_she_has_been_today_and_nothing_made_up(tmp_path):
    home = house(tmp_path)
    assert "你今天去过" not in home.house_note("codex", MEMBERS)  # nowhere yet
    home.begin("codex", "read", "一篇文章")
    home.end("codex")  # finishing where she is is not going anywhere
    home.begin("codex", "private", "聊聊", peer="grok", peer_home=True)
    note = home.house_note("codex", MEMBERS)
    today = note.split("你今天去过：")[1]
    assert today.count("\n- ") == 2
    assert "书房的电脑桌旁 · 在看东西" in today and "（找Grok）" in today


def test_old_moves_are_not_today_and_only_so_many_are_kept(tmp_path):
    home = house(tmp_path)
    home.moves["codex"] = [{"ts": 0.0, "room": "study", "verb": "read", "piece": "", "peer": ""}]
    assert "你今天去过" not in home.house_note("codex", MEMBERS)
    for i in range(MOVES_KEPT + 10):
        home.begin("codex", "read" if i % 2 else "watch", str(i))
    assert len(home.moves["codex"]) == MOVES_KEPT
    assert len(Home(tmp_path).moves["codex"]) == MOVES_KEPT  # saved with the house


async def test_every_turn_carries_the_house_note(make, round_cfg, store):
    a = make("a", ["hi"])
    room = Conversation("chat", [a], FakeSink(), round_cfg, store, RuleModerator(random.Random(0)),
                        house=lambda pid: f"[家里现在]{pid} 在客厅[/家里现在]")
    room.add_user_message("me", "大家在哪")
    await room.wait_idle()
    assert "[家里现在]a 在客厅[/家里现在]" in a.prompts[0]
