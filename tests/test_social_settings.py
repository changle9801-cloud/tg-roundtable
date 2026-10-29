import importlib.util
import json
from pathlib import Path


def test_apply_backs_up_exact_bytes_and_preserves_personal_settings(tmp_path):
    spec = importlib.util.spec_from_file_location('social_defaults',
        Path(__file__).parents[1] / 'scripts' / 'apply_social_defaults.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    settings = {'prompts': {'system_template': '旧规则', 'scene': '朋友群'},
                'participants': [{'id': 'codex', 'browse_topics': '游戏', 'role': '自定义角色'}]}
    path = tmp_path / 'settings.json'
    original = json.dumps(settings, ensure_ascii=False).encode('utf-8')
    path.write_bytes(original)
    backup = module.apply(tmp_path)
    assert backup.read_bytes() == original
    result = json.loads(path.read_text(encoding='utf-8'))
    assert result['participants'] == settings['participants']
    assert result['prompts']['scene'] == '朋友群'
    assert result['prompts']['system_template'] == module.DEFAULT_SYSTEM_PROMPT
    assert module.apply(tmp_path) is None
