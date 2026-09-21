import json
import sqlite3
from pathlib import Path


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, agent_id TEXT, payload TEXT NOT NULL)')
        self.db.commit()

    def load(self):
        row = self.db.execute('SELECT payload FROM state WHERE id=1').fetchone()
        return json.loads(row[0]) if row else {}

    def save(self, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO state VALUES (1, ?)', (json.dumps(value),))

    def event(self, agent_id, value):
        with self.db:
            cur = self.db.execute('INSERT INTO events(agent_id,payload) VALUES (?,?)', (agent_id, json.dumps(value)))
        return cur.lastrowid

    def events(self, agent_id, after=0):
        rows = self.db.execute('SELECT id,payload FROM events WHERE agent_id=? AND id>? ORDER BY id LIMIT 500', (agent_id, after))
        return [{'id': row[0], **json.loads(row[1])} for row in rows]

    def close(self):
        self.db.close()

