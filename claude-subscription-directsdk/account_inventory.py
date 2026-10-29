"""Select official-CLI configs from an owner-granted connection-app inventory.

Only opaque directory IDs cross this boundary. Never copy or refresh credentials here.
A malformed/empty explicit inventory never falls back to the host's unrelated login.
"""
import json
from pathlib import Path
import re
import threading
import time


class Inventory:
    def __init__(self):
        self.lock = threading.Lock()
        self.cursor = 0
        self.cooldowns = {}

    @staticmethod
    def directories(root):
        root = Path(root).expanduser().resolve()
        catalog = root / 'accounts.json'
        if not catalog.exists():
            return [str(root)]  # previous single-directory contract
        rows = json.loads(catalog.read_text())
        if not isinstance(rows, list) or not rows:
            raise ValueError('Connect a subscription in the Claude connection app')
        paths = []
        for row in rows:
            ident = row.get('id') if isinstance(row, dict) else None
            if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', ident):
                raise ValueError('Invalid connection-app account inventory')
            path = root if ident == 'legacy' else (root / 'accounts' / ident).resolve()
            if path != root and root not in path.parents:
                raise ValueError('Connection-app account directory escapes the granted share')
            if path in paths:
                raise ValueError('Duplicate connection-app account directory')
            paths.append(path)
        return [str(path) for path in paths]

    def select(self, root):
        paths = self.directories(root)
        with self.lock:
            offset = self.cursor % len(paths)
            self.cursor += 1
            ordered = paths[offset:] + paths[:offset]
            return next((p for p in ordered if self.cooldowns.get(p, 0) <= time.monotonic()), ordered[0])

    def rejected(self, directory):
        with self.lock:
            self.cooldowns[directory] = time.monotonic() + 60
