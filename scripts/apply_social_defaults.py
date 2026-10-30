"""Preview or apply the agreed shared chat rules; preserve roles and private data.

Stop the app before applying, then restart it. Without --apply this only previews.
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from nookling.prompts import DEFAULT_SYSTEM_PROMPT
from nookling.settings import resolve_data_dir


def apply(data_dir: Path) -> Path | None:
    path = data_dir / 'settings.json'
    original = path.read_bytes()
    settings = json.loads(original.decode('utf-8'))
    if settings.get('prompts', {}).get('system_template') == DEFAULT_SYSTEM_PROMPT:
        return None
    settings.setdefault('prompts', {})['system_template'] = DEFAULT_SYSTEM_PROMPT
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = path.with_name(f'settings.before-social-{stamp}.json')
    with backup.open('xb') as stream:
        stream.write(original)
    temp = path.with_name('settings.social.tmp')
    temp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)
    return backup


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.apply:
        print(apply(resolve_data_dir(args.data_dir)) or 'Already applied.')
    else:
        print(DEFAULT_SYSTEM_PROMPT)
