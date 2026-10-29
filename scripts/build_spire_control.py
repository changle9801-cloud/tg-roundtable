"""Build the small local bridge addon against the owner's installed game assets."""
import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path

from roundtable.games.spire import ASSETS, exclusive_game, process_alive


def build(root, install):
    game = root / 'games' / 'slay-the-spire'
    setup, runtime = game / 'setup', game / 'runtime'
    repo = Path(__file__).resolve().parents[1]
    with exclusive_game(game):
        active = game / 'active.json'
        if active.exists() and process_alive(json.loads(active.read_text())['pid']):
            raise RuntimeError('Close the isolated game before rebuilding its addon')
        with tempfile.TemporaryDirectory(prefix='addon-build-', dir=setup) as folder:
            classes = Path(folder).resolve()
            assert classes.is_relative_to(setup.resolve())
            classpath = os.pathsep.join(str(runtime / name) for name in ASSETS[:-1])
            subprocess.run([str(install / 'jre/bin/java.exe'), '-jar', str(setup / 'ecj.jar'),
                '-1.8', '-encoding', 'UTF-8', '-classpath', classpath, '-d', str(classes),
                str(repo / 'integrations/slay_the_spire/RoundtableControl.java')], check=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            with zipfile.ZipFile(runtime / ASSETS[-1], 'w', zipfile.ZIP_DEFLATED) as archive:
                for path in classes.rglob('*.class'):
                    archive.write(path, path.relative_to(classes).as_posix())
                archive.write(repo / 'integrations/slay_the_spire/ModTheSpire.json', 'ModTheSpire.json')
        manifest = {name: {'sha256': hashlib.sha256((runtime / name).read_bytes()).hexdigest(),
                           'bytes': (runtime / name).stat().st_size} for name in ASSETS}
        (setup / 'asset-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print('Built', runtime / ASSETS[-1])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--install-dir', type=Path, required=True)
    args = parser.parse_args()
    build(args.data_dir, args.install_dir)
