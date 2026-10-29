"""CommunicationMod stdin/stdout bridge; private files connect it to Roundtable.

The game starts this process. Stdout is reserved for the game protocol. Commands
must match the current process generation and observation, so stale files cannot
play a card in a later fight or another member's run.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path


COMMAND = re.compile(r"(?:state|end|proceed|return|resume|pause_run|quit|capture|start (?:ironclad|silent|defect|watcher)(?: [0-9]{1,2})?|"
                     r"play [1-9][0-9]?(?: [0-9]+)?|choose [0-9]+|potion (?:use|discard) [0-9]+(?: [0-9]+)?)", re.I)


def atomic_json(path: Path, value) -> None:
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    # Readers close their handle immediately; Windows can briefly delay replacement.
    for attempt in range(10):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.02)


def command_for(request: dict, session: str, sequence: int) -> str | None:
    action = request.get('command', '')
    if (request.get('session') != session or request.get('sequence') != sequence
            or not isinstance(action, str) or not COMMAND.fullmatch(action)):
        return None
    return action.lower()


def main(directory: Path, session: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    incoming = queue.Queue()

    def read_game():
        try:
            for line in sys.stdin:
                incoming.put(json.loads(line))
        finally:
            incoming.put(None)

    threading.Thread(target=read_game, daemon=True).start()
    sequence, ready, sent = 0, False, False
    print('ready', flush=True)
    while True:
        try:
            payload = incoming.get(timeout=0.05)
        except queue.Empty:
            payload = ...
        if payload is None:
            return
        if payload is not ...:
            sequence += 1
            ready, sent = bool(payload.get('ready_for_command')), False
            atomic_json(directory / 'state.json', {'session': session, 'sequence': sequence,
                        'received_at': time.time(), 'payload': payload, 'bridge_pid': os.getpid()})
        command_file = directory / 'command.json'
        if ready and not sent and command_file.exists():
            try:
                request = json.loads(command_file.read_text(encoding='utf-8'))
                action = command_for(request, session, sequence)
            except (OSError, ValueError, AttributeError):
                action = None
            command_file.unlink(missing_ok=True)
            if action:
                print(action, flush=True)
                # Capture is a read-only framebuffer request and sends no game state.
                sent = action != 'capture'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--session', required=True)
    args = parser.parse_args()
    main(args.directory, args.session)
