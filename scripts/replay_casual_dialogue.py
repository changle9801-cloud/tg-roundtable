"""Compare two-turn conversations in isolated model sessions, with no Telegram sink.

The snapshot holds the before settings, rendered system/voice prompts and public/private
memory. Candidate settings are evaluated against that same snapshot, not moving live data.
Models and credentials still come from the configured accounts. No production sessions
or chat histories are opened for writing.
"""
import argparse
import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from nookling.chronicle import Chronicle
from nookling.life import LifeStore
from nookling.memes import MEME_INSTRUCTIONS, MemeLibrary
from nookling.participants import SessionStore, participant_class
from nookling.prompts import build_system_prompt, build_turn_prompt, render_role
from nookling.runtime import Runtime
from nookling.settings import build_config
from nookling.transcript import Message, USER


CORRECTION = '刚才是我没对上私聊那段，你没记错。后来查了资料，生火沉没是 Aspidochelone 那边的情节，和 Hafgufa 的故事混在一起了，生火那条不能放在 Hafgufa 底下。'
CASES = {
    'checkin': [[Message(USER, '阿明', '你在忙啥呢')],
                [Message(USER, '阿明', '我就随口问问，没让你干啥。')]],
    'hangout': [[Message(USER, '阿明', '今天什么也不想干，就想随便跟你们聊两句')],
                [Message(USER, '阿明', '也没什么烦心事，就是有点无聊。')]],
    'banter': [[Message(USER, '阿明', '刚才找手机找半天，结果就在我手上。')],
               [Message(USER, '阿明', '我还拿它照桌子底下。')]],
    'correction': [[Message('claude', 'Claude', CORRECTION)],
                   [Message(USER, '阿明', '嗯，那就按现在查到的说法记吧。')]],
    'explanation': [[Message('claude', 'Claude', CORRECTION),
                     Message(USER, '阿明', '我没看懂她纠正的是哪一点，给我讲清楚就行。')],
                    [Message(USER, '阿明', '所以是两个故事混了？')]],
    'small_annoyance': [[Message(USER, '阿明', '刚泡好的茶忘了喝，现在全凉了。')],
                        [Message(USER, '阿明', '没事，我其实也爱喝凉的。')]],
    'preference': [[Message(USER, '阿明', '这两张壁纸，一张全黑，一张满屏小星星。你喜欢哪个？')],
                   [Message(USER, '阿明', '我还是选黑的吧，看着清静。')]],
    'clarified_intent': [[Message(USER, '阿明', '我就是今天话少了点，没不高兴。')],
                         [Message(USER, '阿明', '嗯，你们聊，我看着。')]],
    'rain': [[Message(USER, '阿明', '刚想出门就下雨了。')],
             [Message(USER, '阿明', '不出去了，坐着听会雨。')]],
}

DEFAULT_CASES = ('checkin', 'hangout', 'banter', 'correction', 'explanation')


async def main(root, snapshot, label, variants, case_names, resume_before_run=None):
    if resume_before_run:
        assert variants == ['after'] and len(case_names) == 1, 'Resume one scenario at a time'
    settings = json.loads((snapshot / 'settings.json').read_text(encoding='utf-8'))
    candidate = json.loads((snapshot / 'candidate-settings.json').read_text(encoding='utf-8'))
    secrets = json.loads((root / 'secrets.json').read_text(encoding='utf-8'))
    before_cfg, after_cfg = [build_config(value, secrets, root) for value in (settings, candidate)]
    life, memes, chronicle = LifeStore(snapshot), MemeLibrary(snapshot), Chronicle(snapshot)
    output = snapshot / label
    output.mkdir(exist_ok=False)
    results = []
    semaphore = asyncio.Semaphore(2)

    async def run(pid, variant, case):
        async with semaphore:
            cfg = before_cfg if variant == 'before' else after_cfg
            original = next(p for p in before_cfg.participants if p.id == pid)
            pc = next(p for p in cfg.participants if p.id == pid)
            system = (snapshot / (pid + '-before-system.txt')).read_text(encoding='utf-8')
            old_voice = (snapshot / (pid + '-before-voice.txt')).read_text(encoding='utf-8')
            if variant == 'after':
                others = [p.name for p in cfg.enabled_participants if p.id != pid]
                old_rules = build_system_prompt(pc.name, others, role='__rules__', template=settings['prompts']['system_template']).removeprefix('__rules__\n\n')
                new_rules = build_system_prompt(pc.name, others, role='__rules__', template=candidate['prompts']['system_template']).removeprefix('__rules__\n\n')
                for old, new in ((render_role(original), render_role(pc)),
                                 (old_rules, new_rules),
                                 (memes.prompt_section(), MEME_INSTRUCTIONS), (chronicle.prompt_section(), '')):
                    if old:
                        assert old in system, 'Snapshot mismatch: re-capture the baseline'
                        system = system.replace(old, new)
            folder = (snapshot / resume_before_run / f'{pid}-before-checkin' if resume_before_run
                      else output / f'{pid}-{variant}-{case}')
            if resume_before_run:
                assert folder.is_dir(), 'Only existing isolated baseline sessions may be resumed'
            sessions = SessionStore(folder / 'sessions.json')
            previous_session = sessions.get('isolated-casual-replay', pid)
            member = participant_class(pc)(replace(pc, web_search=False), sessions, folder, 150)
            member.set_system_prompt(system)
            names = {p.id: p.name for p in cfg.participants}
            context = SimpleNamespace(life=life, config=cfg, memes=memes, chronicle=chronicle)
            if variant == 'before':
                member.turn_context = lambda prompt: '\n\n'.join(part for part in (
                    life.private_recall(pid, prompt, names), life.artifact_recall(pid, prompt),
                    life.progress_recall(pid, prompt), old_voice) if part)
            else:
                member.turn_context = lambda prompt: Runtime._chat_context(context, pc, prompt)
            record = {'member': pid, 'variant': variant, 'case': case, 'turns': []}
            if resume_before_run:
                record['resumed_from'] = str(folder)
            try:
                for messages in CASES[case]:
                    prompt = build_turn_prompt(messages, 1, me=pid)
                    reply = await member.reply('isolated-casual-replay', prompt)
                    turn = {'input': [m.to_json() for m in messages], 'reply': reply}
                    record['turns'].append(turn)
                    print(json.dumps({'member': pid, 'variant': variant, 'case': case,
                                      'turn': len(record['turns']), 'reply': reply}, ensure_ascii=False), flush=True)
            except Exception as error:
                record['error'] = type(error).__name__ + ': ' + str(error)[:300]
                print(json.dumps({'member': pid, 'variant': variant, 'case': case, 'error': record['error']}, ensure_ascii=False), flush=True)
            results.append(record)
            if previous_session:
                record['session_preserved'] = previous_session == sessions.get('isolated-casual-replay', pid)
            (output / 'replies.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')

    await asyncio.gather(*(run(pid, variant, case) for case in case_names
                           for pid in ('grok', 'deepseek') for variant in variants))
    print(json.dumps({'results': str(output / 'replies.json'), 'cases': len(results),
                      'calls': sum(len(r['turns']) for r in results), 'not_sent_to_telegram': True}), flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-dir', type=Path, required=True)
    p.add_argument('--snapshot', type=Path, required=True)
    p.add_argument('--label', required=True)
    p.add_argument('--variants', nargs='+', choices=('before', 'after'), default=['before', 'after'])
    p.add_argument('--cases', nargs='+', choices=tuple(CASES), default=list(DEFAULT_CASES))
    p.add_argument('--resume-before-run', help='Continue the isolated before/checkin sessions from this prior run')
    args = p.parse_args()
    asyncio.run(main(args.data_dir, args.snapshot, args.label, args.variants, args.cases, args.resume_before_run))
