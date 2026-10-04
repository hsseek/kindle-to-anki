"""Small, indexed import ledger; never stores definitions or book text."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3


def lookup_tag(entry):
    digest = hashlib.sha256(f"{entry.book_id}\0{entry.lookup_id}".encode()).hexdigest()[:24]
    return f"kindle_lookup_{digest}"


class ImportHistory:
    def __init__(self, path, config):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        target = json.dumps([config[k] for k in ("anki_url", "profile", "model_name", "word_field")], ensure_ascii=False)
        self.scope = hashlib.sha256(target.encode()).hexdigest()
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS targets (scope TEXT PRIMARY KEY, target TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS completed (
                scope TEXT NOT NULL, lookup_tag TEXT NOT NULL, word_key TEXT NOT NULL,
                note_id INTEGER, source_timestamp INTEGER NOT NULL, kind TEXT NOT NULL,
                PRIMARY KEY(scope, lookup_tag)
            );
            CREATE INDEX IF NOT EXISTS completed_words ON completed(scope, word_key);
            CREATE TABLE IF NOT EXISTS attempts (
                scope TEXT NOT NULL, lookup_tag TEXT NOT NULL, word_key TEXT NOT NULL,
                source_timestamp INTEGER NOT NULL, PRIMARY KEY(scope, lookup_tag)
            );
            CREATE INDEX IF NOT EXISTS attempted_words ON attempts(scope, word_key);
        """)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO targets VALUES(?, ?)", (self.scope, target))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    @contextmanager
    def exclusive(self):
        # Keep two CLI processes from adding the same pending word concurrently.
        with self.path.with_name(self.path.name + ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError("Another import/history seed is using this history file. Try again after it finishes.") from error
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def contains(self, entry):
        return self.db.execute("""SELECT 1 FROM completed WHERE scope=? AND lookup_tag=?
            UNION ALL SELECT 1 FROM completed WHERE scope=? AND word_key=? LIMIT 1""",
            (self.scope, lookup_tag(entry), self.scope, entry.key)).fetchone() is not None

    def is_uncertain(self, entry):
        return self.db.execute("""SELECT 1 FROM attempts WHERE scope=? AND lookup_tag=?
            UNION ALL SELECT 1 FROM attempts WHERE scope=? AND word_key=? LIMIT 1""",
            (self.scope, lookup_tag(entry), self.scope, entry.key)).fetchone() is not None

    def mark_attempt(self, entry):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO attempts VALUES(?, ?, ?, ?)",
                            (self.scope, lookup_tag(entry), entry.key, entry.timestamp))

    def record(self, entry, note_id=None, kind="imported"):
        self.record_values(lookup_tag(entry), entry.key, entry.timestamp, note_id, kind)

    def record_values(self, tag, word, timestamp, note_id, kind="imported"):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO completed VALUES(?, ?, ?, ?, ?, ?)",
                            (self.scope, tag, word, note_id, timestamp, kind))
            self.db.execute("DELETE FROM attempts WHERE scope=? AND lookup_tag=?", (self.scope, tag))

    def pending_attempts(self):
        return self.db.execute("SELECT lookup_tag, word_key, source_timestamp FROM attempts WHERE scope=?",
                               (self.scope,)).fetchall()

    def clear_attempt(self, tag):
        with self.db:
            self.db.execute("DELETE FROM attempts WHERE scope=? AND lookup_tag=?", (self.scope, tag))

    def seed(self, entries, before):
        count = 0
        with self.db:
            for entry in entries:
                if entry.timestamp >= before or not entry.key:
                    continue
                cursor = self.db.execute("INSERT OR IGNORE INTO completed VALUES(?, ?, ?, NULL, ?, 'seeded')",
                                         (self.scope, lookup_tag(entry), entry.key, entry.timestamp))
                count += cursor.rowcount
                self.db.execute("DELETE FROM attempts WHERE scope=? AND lookup_tag=?", (self.scope, lookup_tag(entry)))
        return count

    def stats(self):
        lookups, words = self.db.execute("SELECT count(*), count(DISTINCT word_key) FROM completed WHERE scope=?",
                                        (self.scope,)).fetchone()
        kinds = dict(self.db.execute("SELECT kind,count(*) FROM completed WHERE scope=? GROUP BY kind", (self.scope,)))
        return {"completed_lookups": lookups, "completed_words": words,
                "seeded_lookups": kinds.get("seeded", 0), "imported_lookups": kinds.get("imported", 0),
                "uncertain_attempts": len(self.pending_attempts())}
