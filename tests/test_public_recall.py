from test_life import runtime

from nookling.chronicle import Chronicle
from nookling.memes import MemeLibrary
from nookling.prompts import build_turn_prompt
from nookling.transcript import Message


def test_casual_checkin_does_not_receive_the_whole_joke_and_anecdote_archive(tmp_path):
    r = runtime(tmp_path)
    r.memes.add('「空碗」是以前开吃白饭玩笑时用过的说法')
    r.chronicle.record('2026-10-28', [{'title': '全群审计吃白饭', 'what': '上次问近况时大家拿吃白饭开玩笑'}])
    r.refresh_prompts()
    actor = r._member('claude')
    assert '全群审计' not in actor.system_prompt and '空碗' not in actor.system_prompt
    current = actor.with_turn_context(build_turn_prompt([Message('user', '主人', '你在忙啥呢')], 1, me=actor.id))
    assert '全群审计' not in current and '空碗' not in current
    assert '全群审计' in r.system_prompt_for(actor.cfg, background=True)
    assert len(r.memes.items) == 1 and len(r.chronicle.moments) == 1


def test_specific_old_joke_and_anecdote_can_still_be_recalled_after_reload(tmp_path):
    lib = MemeLibrary(tmp_path)
    lib.add('「空碗」是以前开吃白饭玩笑时用过的说法')
    lib.add('「冰块加班」是烘干衣服时的玩笑')
    off = lib.add('「秘密暗号」停用后不应送入聊天')
    lib.update(off.id, {'enabled': False})
    loaded = MemeLibrary(tmp_path)
    recall = loaded.recall('你们上次说的空碗是什么？')
    assert '吃白饭' in recall and '冰块加班' not in recall
    assert loaded.recall('秘密暗号') == ''
    c = Chronicle(tmp_path)
    c.record('2026-10-28', [{'title': '洗衣液咖啡', 'what': '主人把洗衣液倒进咖啡机'}])
    assert '洗衣液咖啡' in Chronicle(tmp_path).recall('上次洗衣液倒进咖啡机是怎么回事')
    assert c.recall('今天也在啊，吃了吗') == ''


def test_member_name_or_style_instruction_does_not_select_old_jokes(tmp_path):
    r = runtime(tmp_path)
    r.memes.add('「Claude」是总把煎蛋烧焦的那个人')
    r.memes.add('「空碗」是以前开吃白饭玩笑时用过的说法')
    r.chronicle.record('2026-10-28', [{'title': 'Claude 烧焦了煎蛋', 'what': 'Claude 和 Codex 一起重新做早饭'}])
    prompt = build_turn_prompt([Message('user', '主人', 'Claude 今天在吗')], 1)
    prompt += '\n[测试用附加说明]不要套用空碗笑话，也不要说煎蛋烧焦。'
    recalled = r._chat_context(r._member('claude').cfg, prompt)
    assert '[当前话题提到的旧梗]' not in recalled
    assert '[当前话题相关的群聊往事]' not in recalled
