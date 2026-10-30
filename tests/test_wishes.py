import random

import pytest

from nookling import redecorate, wishes
from nookling.conversation import Conversation
from nookling.furniture import PlacementError
from nookling.home import Home
from nookling.moderator import RuleModerator
from test_web import admin, login  # noqa: F401  (the admin fixture)
from conftest import FakeSink

MEMBERS = {"codex": "Codex", "grok": "Grok"}


def house(tmp_path):
    home = Home(tmp_path)
    home.ensure(MEMBERS)
    return home


def test_the_wish_tag_is_taken_out_of_a_reply():
    text, found = wishes.extract_wishes("好啊[心愿:想要一台咖啡机|早上起不来] 就这样 [心愿：一间书房]")
    assert text == "好啊 就这样" and found == [("想要一台咖啡机", "早上起不来"), ("一间书房", "")]


def test_three_open_wishes_at_most(tmp_path):
    home = house(tmp_path)
    for what in ("一间书房", "一台咖啡机", "一盏台灯"):
        wishes.add(home, "codex", what)
    with pytest.raises(PlacementError, match="3 条心愿"):
        wishes.add(home, "codex", "一张吊床")
    wishes.add(home, "grok", "一张吊床")  # each her own three


def test_approved_a_new_room_is_built_and_everyone_hears(tmp_path):
    home = house(tmp_path)
    wish = wishes.add(home, "codex", "想要一间自己的书房", "公共书房老被打扰", {"type": "room", "name": "小书房", "size": "small"})
    wishes.answer(home, wish["id"], "approve", note="给你小一点的", names=MEMBERS)
    room = next(r for r in home.rooms if r["name"] == "小书房")
    assert room["size"] == [10, 10] and room["id"] not in home.shut_rooms()  # it has a door
    assert wish["status"] == "done" and "加了一间小书房" in wish["result"]
    told = wishes.news(home, "codex")
    assert "同意了，已经弄好" in told and "给你小一点的" in told and wishes.news(home, "codex") == ""
    assert "按Codex的心愿加了一间小书房" in home.news("grok", MEMBERS)


def test_approved_another_way_by_hand_or_declined(tmp_path):
    home = house(tmp_path)
    tv = next(p for p in home.pieces if p["type"] == "base/tv")
    move = wishes.add(home, "codex", "想把电视搬进卧室", ask={"type": "move_to", "piece": tv["id"], "room": "bed-codex"})
    wishes.answer(home, move["id"], "approve", ask={"type": "add", "room": "bed-codex", "kind": "base/beanbag"}, names=MEMBERS)
    assert tv["room"] == "fun" and any(p["type"] == "base/beanbag" for p in home.pieces if p["room"] == "bed-codex")
    assert move["changed"] and "改了一点" in wishes.news(home, "codex")
    song = wishes.add(home, "codex", "想要一台能放歌的唱片机")
    wishes.answer(home, song["id"], "approve", ask={"type": "manual"})
    assert song["status"] == "approved" and "会去弄" in wishes.news(home, "codex")
    wishes.answer(home, song["id"], "done", note="接好了音乐工具")
    assert song["status"] == "done" and "弄好了" in wishes.news(home, "codex")
    nap = wishes.add(home, "codex", "想要一张吊床")
    with pytest.raises(PlacementError, match="写一句为什么"):
        wishes.answer(home, nap["id"], "decline")
    wishes.answer(home, nap["id"], "decline", note="客厅放不下")
    told = wishes.news(home, "codex")
    assert "没同意" in told and "客厅放不下" in told and "换个说法" in told
    with pytest.raises(PlacementError, match="已经回过"):
        wishes.answer(home, nap["id"], "approve")


def test_she_wishes_while_redecorating_and_it_is_not_counted(tmp_path):
    home = house(tmp_path)
    result = redecorate.apply(home, "codex", {"changes": [
        {"do": "wish", "what": "客厅加盆绿植", "why": "太空了", "ask": {"type": "add", "room": "living", "kind": "base/plant"}}]})
    assert result["wished"] == ["客厅加盆绿植"] and result["done"] == []
    assert redecorate.left_this_week(home, "codex") == 3
    assert "你的心愿：" in redecorate.menu(home, "codex") and "等你回" in redecorate.menu(home, "codex")
    assert "你现在有 1 条在等" in home.house_note("codex", MEMBERS)


async def test_a_wish_said_in_chat_goes_on_the_list(make, round_cfg, store):
    got = []
    a = make("a", ["好主意 [心愿:想要一台咖啡机|早上要提神]"])
    room = Conversation("chat", [a], FakeSink(), round_cfg, store, RuleModerator(random.Random(0)),
                        on_wish=lambda p, w: got.append((p.id, w)))
    room.add_user_message("me", "家里缺什么")
    await room.wait_idle()
    assert got == [("a", [("想要一台咖啡机", "早上要提神")])] and room.messages[-1].text == "好主意"


async def test_the_page_answers_a_wish(admin):  # noqa: F811
    await login(admin)
    runtime = admin.runtime
    runtime.home.ensure({"codex": "Codex"})
    wish = wishes.add(runtime.home, "codex", "客厅加盆绿植", ask={"type": "add", "room": "living", "kind": "base/plant"})
    view = await (await admin.get("/api/home")).json()
    assert view["wishes"][0]["id"] == wish["id"]
    h = {"Origin": admin.origin}
    ok = await admin.post(f"/api/home/wishes/{wish['id']}/answer", json={"decision": "approve"}, headers=h)
    assert ok.status == 200 and (await ok.json())["wishes"][0]["status"] == "done"
    again = await admin.post(f"/api/home/wishes/{wish['id']}/answer", json={"decision": "approve"}, headers=h)
    assert again.status == 400
