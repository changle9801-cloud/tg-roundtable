from roundtable.home import Home, bedroom

MEMBERS = {"codex": "Codex", "grok": "Grok", "xiaolin": "小林"}


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def house(tmp_path, clock=None):
    home = Home(tmp_path, clock or Clock())
    home.ensure(MEMBERS)
    return home


def test_everyone_gets_a_bedroom_a_look_and_a_place(tmp_path):
    home = house(tmp_path)
    ids = {r["id"] for r in home.rooms}
    assert {"living", "kitchen", "study", "fun", "storage", "bed-codex", "bed-grok", "bed-xiaolin"} <= ids
    assert home.looks["grok"]["accessory"] == "sunglasses"
    assert home.looks["xiaolin"]["color"].startswith("#")  # no default for a new member: a spare colour
    assert home.where["codex"].room == bedroom("codex")


def test_she_goes_where_the_activity_happens_and_stays_after(tmp_path):
    home = house(tmp_path)
    home.begin("codex", "read", "磁吸手柄拆机")
    assert (home.where["codex"].room, home.where["codex"].verb, home.where["codex"].active) == ("study", "read", True)
    home.end("codex")
    place = Home(tmp_path).where["codex"]  # and it survives a restart
    assert (place.room, place.verb, place.active) == ("study", "read", False)
    home.begin("grok", "play")
    assert home.where["grok"].room == "fun"
    home.begin("grok", "idle", "有点困，想回床上躺会儿")
    assert home.where["grok"].room == bedroom("grok")
    home.begin("grok", "idle", "没什么想做的")
    assert home.where["grok"].room == "living"


def test_a_visit_goes_to_the_friend_and_comes_back(tmp_path):
    home = house(tmp_path)
    home.begin("codex", "read")
    home.end("codex")
    home.begin("grok", "private", "聊聊游戏", peer="codex", peer_home=True)
    assert (home.where["grok"].room, home.where["grok"].peer) == ("study", "codex")
    home.end("grok")
    assert home.where["grok"].room == bedroom("grok")
    home.begin("grok", "private", peer="xiaolin", peer_home=False)  # not home or asleep: a phone call
    assert (home.where["grok"].room, home.where["grok"].verb) == (bedroom("grok"), "phone")


def test_routine_puts_her_to_bed_and_out_of_the_house(tmp_path):
    home = house(tmp_path)
    home.begin("codex", "read")
    home.settle("codex", "asleep")
    assert (home.where["codex"].room, home.where["codex"].verb) == (bedroom("codex"), "sleep")
    since = home.where["codex"].since
    home.settle("codex", "asleep")
    assert home.where["codex"].since == since  # idempotent on every tick
    home.settle("codex", "online")
    assert (home.where["codex"].room, home.where["codex"].verb) == (bedroom("codex"), "rest")
    home.settle("codex", "busy")
    assert home.where["codex"].verb == "out"
    home.settle("codex", "online")
    assert home.where["codex"].room == "living"


def test_she_hears_once_which_days_of_her_diary_were_read(tmp_path):
    clock = Clock()
    home = house(tmp_path, clock)
    home.read_diary("codex", "2026-10-29")
    home.read_diary("codex", "2026-10-29")  # opened again right away: the same read
    assert len(home.diary_reads) == 1
    assert home.tell("grok") == ""
    line = home.tell("codex")
    assert "10 月 29 日" in line and "阿明" in line
    assert home.tell("codex") == ""  # only once
    clock.t += 3600
    home.read_diary("codex", "2026-10-29")  # a later read is a new one
    assert "10 月 29 日" in Home(tmp_path).tell("codex")


def test_guests_see_only_the_room_and_a_fixed_word(tmp_path):
    home = house(tmp_path)
    home.begin("codex", "read", "小林在大阪的那家店")
    home.begin("grok", "private", "想问问 Codex 那件事", peer="codex", peer_home=True)
    home.settle("xiaolin", "busy")
    text = home.guest_lines(MEMBERS)
    assert "Codex · 书房 · 在看东西" in text
    assert "Grok · 书房 · 在找朋友聊天" in text
    assert "小林 · 出门了" in text
    assert "大阪" not in text and "那件事" not in text
    home.end("codex")
    assert "Codex · 书房 · 在歇着" in home.guest_lines(MEMBERS)


def test_looks_only_take_listed_accessories_and_real_colours(tmp_path):
    home = house(tmp_path)
    assert home.set_look("grok", "#AABBCC", "hat") == {"color": "#aabbcc", "accessory": "hat"}
    for colour, accessory in (("red", "hat"), ("#aabbcc", "crown")):
        try:
            home.set_look("grok", colour, accessory)
        except ValueError:
            continue
        raise AssertionError("accepted a bad look")


def test_a_broken_file_starts_the_house_afresh_and_keeps_the_file(tmp_path):
    (tmp_path / "home.json").write_text("{not json", encoding="utf-8")
    home = house(tmp_path)
    assert home.where["codex"].room == bedroom("codex")
    assert (tmp_path / "home.broken.json").exists()


# ---- in the running app ----

async def test_a_daily_step_takes_her_to_the_room_and_she_stays(tmp_path):
    from test_life import runtime
    r = runtime(tmp_path)
    r._member("claude").cfg.web_search = True
    seen = []

    async def ask(pid, prompt):
        if not seen:
            seen.append(None)
            return '{"action":"browse","reason":"随便看看"}'
        place = r.home.where["claude"]
        seen.append((place.room, place.verb, place.active))
        return '{"outcome":"nothing_new","note":"没什么新鲜的"}'

    r._life_ask = ask
    r.start_life("claude")
    await r._life_task
    assert seen[1] == ("study", "browse", True)
    place = r.home.where["claude"]
    assert (place.room, place.active) == ("study", False)
    assert "Claude · 书房 · 在歇着" in r.home_text()


def test_opening_a_day_of_her_diary_is_reading_it_and_she_hears_once(tmp_path):
    from test_diary import reading
    from test_life import runtime
    r = runtime(tmp_path)
    r.life.remember_discovery("claude", reading())
    cover = r.diary_book("claude")
    day = cover["days"][0]["day"]
    assert not r.home.diary_reads  # the list of days is not reading
    opened = r.diary_book("claude", day)
    assert opened["entries"] and opened["entries"][0]["member"] == "claude"
    prompt = "[群聊新消息]\nOrion: 今天干嘛了\n[/群聊新消息]"
    assert "阿明翻了你" not in r._chat_context(r._member("codex").cfg, prompt)
    assert "阿明翻了你" in r._chat_context(r._member("claude").cfg, prompt)
    assert "阿明翻了你" not in r._chat_context(r._member("claude").cfg, prompt)
    assert "翻了哪天的，你都会知道" in r.system_prompt_for(r._member("claude").cfg)


async def test_she_picks_her_own_look_from_the_list(tmp_path):
    from test_life import runtime
    import pytest
    from roundtable.config import ConfigError
    r = runtime(tmp_path)
    replies = ['{"color":"#123456","accessory":"bow","why":"想可爱一点"}', '{"color":"#123456","accessory":"crown"}']

    async def ask(pid, prompt):
        assert "配饰只能从这些里选" in prompt
        return replies.pop(0)

    r._life_ask = ask
    assert (await r.choose_look("claude"))["accessory"] == "bow"
    assert r.home.looks["claude"] == {"color": "#123456", "accessory": "bow"}
    with pytest.raises(ConfigError, match="她挑的不行"):
        await r.choose_look("claude")
    assert r.home.looks["claude"]["accessory"] == "bow"
