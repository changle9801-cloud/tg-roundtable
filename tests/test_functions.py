import json

from nookling import furniture
from nookling.conversation import Conversation
from nookling.participants.base import ToolActivity
from nookling.participants.claude_cli import _activity_reader
from nookling.participants.grok_cli import describe_tool
from nookling.runtime import Runtime
from nookling.settings import SettingsStore, normalize_settings

TOOLS = [
    {"id": "video", "name": "看视频", "transport": "stdio", "command": "python", "allowed": ["watch_video"], "members": ["claude"]},
    {"id": "music", "name": "听歌", "transport": "stdio", "command": "python", "allowed": ["play_song"], "members": ["claude"]},
]
MEMBERS = [{"id": "claude", "name": "Claude", "kind": "claude_cli", "command": ["python"]}]


def runtime(tmp_path, **settings):
    store = SettingsStore(tmp_path)
    store.save({"participants": MEMBERS, "tools": TOOLS, **settings})
    return Runtime(store)


def parcels(r):
    return [p for p in r.home.pieces if p["type"] == "base/parcel"]


def test_tool_calls_say_which_tool_they_used():
    seen = []
    read = _activity_reader(seen.append, {"video": "看视频"})
    read(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "mcp__video__watch_video", "input": {}}]}}))
    assert seen == ["正在用看视频"] and seen[0].server == "video"
    grok = describe_tool({"type": "tool_call", "toolName": "use_tool", "rawInput": {"server": "music"}})
    assert grok == "正在用工具" and grok.server == "music"
    assert not hasattr(describe_tool({"type": "tool_call", "toolName": "use_tool"}), "server")  # Grok did not say


def test_links_start_from_what_each_tool_obviously_does():
    links = normalize_settings({"tools": TOOLS})["functions"]
    assert links == {"video.watch": ["tool:video"]}  # the music tool has no built-in function to join
    assert furniture.tool_functions(links, "music") == ["tool.music"]
    kept = normalize_settings({"tools": TOOLS, "functions": {"music.play": ["tool:music", "tool:gone"]}})["functions"]
    assert kept == {"music.play": ["tool:music"]}


async def test_an_unlinked_tool_waits_in_a_box_until_it_is_linked(tmp_path):
    r = runtime(tmp_path)
    box, = parcels(r)
    assert box["room"] == "storage" and box["name"] == "还没拆箱：听歌" and box["functions"] == ["tool.music"]
    r._tool_used("claude", "music", during_chat=True)  # she uses it before it is unpacked: to the box
    assert r.home.where["claude"].piece == box["id"] and r.home.where["claude"].verb == "use"
    assert not r.home.where["claude"].active
    settings = {**r.settings, "functions": {**r.settings["functions"], "video.watch": ["tool:video", "tool:music"]}}
    await r.apply(settings=settings)
    assert parcels(r) == []  # unpacked: it now does what the TV does
    r._tool_used("claude", "music")
    tv = next(p for p in r.home.pieces if p["type"] == "base/tv")
    assert (r.home.where["claude"].piece, r.home.where["claude"].verb) == (tv["id"], "watch")


async def test_furniture_whose_tool_is_off_is_grey(tmp_path):
    r = runtime(tmp_path)
    assert "video.watch" in r.home_view()["available"]
    off = [{**t, "enabled": t["id"] != "video"} for t in r.settings["tools"]]
    await r.apply(settings={**r.settings, "tools": off})
    available = r.home_view()["available"]
    assert "video.watch" not in available and "web.read" not in available  # no reader tool set up here
    assert "game.play" in available  # built in, always there


async def test_using_a_tool_in_the_group_walks_her_to_its_furniture(tmp_path, make, store, round_cfg):
    r = runtime(tmp_path)
    member = make("claude", ["看完了，挺好笑"])
    member.activity = ToolActivity("正在用看视频", "video")
    used = []
    convo = Conversation("-100", [member], r.sink, round_cfg, store,
                         on_tool=lambda p, server: (used.append(server), r._tool_used(p.id, server, during_chat=True)))
    convo.add_user_message("主人", "@claude 看看这个视频 https://example.com/v")
    await convo.wait_idle()
    tv = next(p for p in r.home.pieces if p["type"] == "base/tv")
    assert used == ["video"] and r.home.where["claude"].piece == tv["id"] and r.home.where["claude"].room == "fun"


def test_she_does_not_walk_home_from_outside_or_out_of_bed(tmp_path):
    r = runtime(tmp_path)
    r.home.settle("claude", "busy")
    r._tool_used("claude", "video", during_chat=True)
    assert r.home.where["claude"].verb == "out"
    r.home.settle("claude", "asleep")
    r._tool_used("claude", "video")
    assert r.home.where["claude"].verb == "sleep"
