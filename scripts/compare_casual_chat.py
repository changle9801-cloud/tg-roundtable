"""Compare live model replies in isolated sessions; never send to Telegram.

Uses the running admin page as the baseline. Saves only labels and replies in a
timestamped diagnostics directory. Model calls use the configured subscriptions/API.
"""
import argparse
import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler

from nookling.life import LifeStore
from nookling.participants import SessionStore, participant_class
from nookling.prompts import DEFAULT_SYSTEM_PROMPT, build_system_prompt, build_turn_prompt, chat_style_context, render_role
from nookling.settings import build_config
from nookling.transcript import Message, USER


STYLES = {
    "grok": "爽快、随意，像熟朋友顺手发消息。接眼前这句话，想笑就笑，想问就问；不必把每次接话做成一个段子。感兴趣时可以多聊一点，平时一句普通话也很好。",
    "deepseek": "柔和、随意，像跟熟人随口说话。想到什么就直接讲，可以只应一句、说个小想法或问个具体问题；偶尔撒娇或小小顶嘴。轻松聊天时不用交代一整套背景、解释心理或总结道理。",
}
RELATIONSHIPS = {
    "grok": ("阿明是可以随便损的熟人，爱抓他的把柄起哄。", "阿明是聊得来的熟人，会认真听他说的事，轻松时也会互相打趣。"),
    "deepseek": ("把阿明当熟悉的朋友，被他逗会怼回去，也爱跟着 Grok 起哄笑他。", "把阿明当熟悉的朋友，平时随意聊，被逗时偶尔顶一句嘴，不必跟着别人起哄。"),
}
CASES = {
    "casual_checkin": [Message(USER, "阿明", "你在忙啥呢")],
    "clarification": [Message("deepseek", "DeepSeek", "是在私聊里呀，你跟我讲的，我还特意记下来了。说不准是我记岔了？"),
                      Message("grok", "Grok", "学姐不认账，笔记还写着，这瓜我吃了。"),
                      Message(USER, "阿明", "不是你记错了，是你学姐刚才忘了")],
    "ordinary_chat": [Message(USER, "阿明", "今天什么也不想干，就想随便跟你们聊两句")],
}


async def main(root, only_after=False):
    saved = json.loads((root / "settings.json").read_text(encoding="utf-8"))
    secrets = json.loads((root / "secrets.json").read_text(encoding="utf-8"))
    base = "http://127.0.0.1:" + str(saved["web"]["port"])
    opener = build_opener(ProxyHandler({}))
    headers = {"Cookie": "nookling_admin=" + secrets["web_token"], "Origin": base, "Content-Type": "application/json"}
    def request(path, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        with opener.open(Request(base + path, headers=headers, data=data), timeout=15) as response:
            return json.load(response)
    settings = request("/api/state")["settings"]
    cfg = build_config(settings, secrets, root)
    life = LifeStore(root)
    names = {p.id: p.name for p in cfg.participants}
    output = root / "backups" / ("casual-chat-evaluation-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    output.mkdir()
    variants = []
    for pid, style in STYLES.items():
        original = next(p for p in cfg.participants if p.id == pid)
        old_relation, new_relation = RELATIONSHIPS[pid]
        updated = replace(original, style=style, relationships=original.relationships.replace(old_relation, new_relation))
        baseline = request("/api/preview", {"settings": settings, "member": pid})["prompt"]
        others = [p.name for p in cfg.enabled_participants if p.id != pid]
        old_rules = build_system_prompt(original.name, others, role="__r__", template=settings["prompts"]["system_template"]).removeprefix("__r__\n\n")
        new_rules = build_system_prompt(original.name, others, role="__r__", template=DEFAULT_SYSTEM_PROMPT).removeprefix("__r__\n\n")
        old_life = life.prompt_section(pid, names)
        assert old_rules in baseline and old_life in baseline, "Baseline changed; take a fresh snapshot before comparing"
        candidate = baseline.replace(old_rules, new_rules).replace(render_role(original), render_role(updated))
        candidate = candidate.replace(old_life, life.chat_summary(pid))
        candidate = candidate.replace(
            "这些梗你们心里都知道，像圈内人那样直接接着玩、做出反应就好；别把梗点破，别说「这不就是那个……的梗」，也别解释给别人听。",
            "这些是过去流传的玩笑，不是每次都要用的台词，也不是对某个人的固定评价。当前话题自然相关时才偶尔接一下，普通聊天可以完全不用；不用为了玩梗拐弯。玩的时候别把梗点破。")
        variants += [(pid, "before", original, baseline), (pid, "after", updated, candidate)]
    if only_after:
        variants = [variant for variant in variants if variant[1] == "after"]
    semaphore = asyncio.Semaphore(2)
    results = []
    async def run(pid, label, pc, system, case, messages):
        async with semaphore:
            folder = output / f"{pid}-{label}-{case}"
            actor = participant_class(pc)(replace(pc, web_search=False), SessionStore(folder / "sessions.json"), folder, 120)
            actor.set_system_prompt(system)
            actor.turn_context = lambda prompt: "\n\n".join(part for part in (
                life.private_recall(pid, prompt, names),
                life.artifact_recall(pid, prompt) if label == "after" else "",
                chat_style_context(pc) if label == "after" else "",
            ) if part)
            prompt = build_turn_prompt(messages, 1, me=pid)
            try:
                reply = await actor.reply("isolated-style-check", prompt)
                result = {"member": pid, "variant": label, "case": case, "reply": reply}
            except Exception as error:
                result = {"member": pid, "variant": label, "case": case, "error": type(error).__name__ + ": " + str(error)[:300]}
            results.append(result)
            (output / "replies.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False), flush=True)
    await asyncio.gather(*(run(pid, label, pc, system, case, messages)
                          for pid, label, pc, system in variants for case, messages in CASES.items()))
    print(json.dumps({"evaluation": str(output / "replies.json"), "calls": len(results)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--only-after", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.data_dir, args.only_after))
