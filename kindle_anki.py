#!/usr/bin/env python3
"""Import Kindle Vocabulary Builder words through AnkiConnect (stdlib only)."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime
import html
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.request

from kotobank import Definition, DictionaryUnavailable, Kotobank
from import_history import ImportHistory, lookup_tag


DEFAULTS = {
    "profile": "sun",
    "model_name": "Japanese-words",
    "deck_name": "Kindle",
    "word_field": "Word",
    "definition_field": "Description",
    "sentence_field": "Additional",
    "source_field": "References",
    "anki_url": "http://127.0.0.1:8765",
    "language": "ja",
}
MARKS = str.maketrans("", "", "‐‑–・△▽×＝")
DEFAULT_HISTORY = Path(__file__).with_name("import-history.sqlite3")


class ImportErrorMessage(Exception):
    """An actionable error, suitable for display without a traceback."""


class AnkiUnavailableError(ImportErrorMessage):
    """The result of an Anki request may be uncertain."""


@dataclass(frozen=True)
class Entry:
    lookup_id: str
    word: str
    stem: str
    sentence: str
    title: str
    authors: str
    timestamp: int
    category: int
    book_id: str

    @property
    def expression(self):
        return dictionary_expression(self.stem, self.word)

    @property
    def key(self):
        return word_key(self.expression)


def word_key(text):
    """Recognize existing Anki ruby notation as the same written word."""
    text = re.sub(r"<rt\b[^>]*>.*?</rt>", "", text, flags=re.S | re.I)
    text = html.unescape(re.sub(r"<[^>]*>", "", text))
    text = re.sub(r"\[[^\]]*\]", "", text)
    return unicodedata.normalize("NFC", re.sub(r"\s+", "", text))


def dictionary_expression(stem, surface):
    """Read Daijisen's recorded headword/reading, without a MeCab dependency."""
    match = re.match(r"^(.*?)【(.*?)】", stem or "")
    if not match:
        plain = (stem or "").translate(MARKS).strip()
        if re.fullmatch(r"[ぁ-ゖァ-ヺー\u3400-\u9fff]+", plain):
            if re.search(r"[\u3400-\u9fff]", plain) or not re.search(r"[\u3400-\u9fff]", surface):
                return plain
        return surface.strip()
    head = re.split(r"[／/]", match[1])[0].translate(MARKS).strip()
    reading = re.split(r"[／/]", match[2])[0].translate(MARKS).strip()
    # Daijisen also stores entries in reading-first order: くし【串】.
    kanji = r"[\u3400-\u9fff]"
    if not re.search(kanji, head) and re.search(kanji, reading):
        head, reading = reading, head
    if not head:
        return surface.strip()
    if reading and re.fullmatch(r"[ぁ-ゖァ-ヺー]+", reading) and head != reading:
        return f"{head}[{reading}]"
    return head


def load_config(path):
    config = dict(DEFAULTS)
    if path.exists():
        supplied = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(supplied, dict):
            raise ImportErrorMessage("Config must be a JSON object.")
        config.update(supplied)
    elif path != Path(__file__).with_name("config.json"):
        raise ImportErrorMessage(f"Config not found: {path}")
    for key in DEFAULTS:
        if not isinstance(config[key], str) or not config[key].strip():
            raise ImportErrorMessage(f"Config value {key!r} must be a nonempty string.")
    mapped = [config[k] for k in ("word_field", "definition_field", "sentence_field", "source_field")]
    if len(set(mapped)) != len(mapped):
        raise ImportErrorMessage("Each content field must map to a different Anki field.")
    return config


def find_database(explicit=None):
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_dir():
            path = path / "system/vocabulary/vocab.db"
        if not path.is_file():
            raise ImportErrorMessage(f"Vocabulary database not found: {path}")
        return path
    candidates = []
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    patterns = [(runtime / "gvfs", "mtp:host=*Kindle*/Internal Storage/system/vocabulary/vocab.db")]
    for base in (Path("/media") / Path.home().name, Path("/run/media") / Path.home().name):
        patterns.append((base, "*/system/vocabulary/vocab.db"))
    for base, pattern in patterns:
        candidates.extend(base.glob(pattern))
    candidates = sorted(set(p for p in candidates if p.is_file()))
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        raise ImportErrorMessage("Multiple Kindles found; select one with --kindle PATH.")
    raise ImportErrorMessage("No mounted Kindle found. Connect/unlock it and open it in Files, "
                             "or pass --kindle /path/to/vocab.db. Local ~/.kindle copies are not used automatically.")


def read_entries(path, language):
    # GVfs/MTP cannot reliably satisfy SQLite seeks/locking; copy to local disk first.
    with tempfile.TemporaryDirectory(prefix="kindle-anki-") as directory:
        local = Path(directory) / "vocab.db"
        shutil.copyfile(path, local)
        for suffix in ("-wal",):
            sidecar = path.with_name(path.name + suffix)
            if sidecar.is_file():
                shutil.copyfile(sidecar, local.with_name(local.name + suffix))
        db = sqlite3.connect(local)
        try:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ImportErrorMessage("Kindle database copy is inconsistent. Reconnect the Kindle and retry.")
            rows = db.execute("""
                SELECT l.id, w.word, w.stem, l.usage, b.title, b.authors,
                       l.timestamp, w.category, l.book_key
                FROM LOOKUPS l JOIN WORDS w ON w.id=l.word_key
                LEFT JOIN BOOK_INFO b ON b.id=l.book_key
                WHERE w.lang=? ORDER BY l.timestamp, l.id
            """, (language,)).fetchall()
            return [Entry(r[0], r[1] or "", r[2] or "", r[3] or "",
                          r[4] or "Unknown book", r[5] or "", r[6] or 0,
                          r[7] or 0, r[8] or "") for r in rows]
        finally:
            db.close()


def select_entries(entries, history, book=None):
    selected, seen, counts = [], set(), Counter()
    for entry in entries:
        if book and book.casefold() not in entry.title.casefold():
            continue
        counts["lookups"] += 1
        if not entry.key or not entry.sentence.strip():
            counts["incomplete"] += 1
        elif history.contains(entry):
            counts["already_in_history"] += 1
        elif history.is_uncertain(entry):
            counts["awaiting_confirmation"] += 1
        elif entry.key in seen:
            counts["repeated_lookup"] += 1
        else:
            seen.add(entry.key)
            selected.append(entry)
    counts["new_words"] = len(selected)
    counts["selected"] = len(selected)
    return selected, dict(counts)


def required_fields(config):
    return [config[k] for k in ("word_field", "definition_field", "sentence_field", "source_field")]


def validate_fields(config, fields):
    missing = set(required_fields(config)) - set(fields)
    if missing:
        raise ImportErrorMessage("Anki note type is missing fields: " + ", ".join(sorted(missing)))


class AnkiConnect:
    def __init__(self, url):
        self.url = url
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, action, **params):
        payload = {"action": action, "version": 6, "params": params}
        api_key = os.environ.get("ANKICONNECT_API_KEY")
        if api_key:
            payload["key"] = api_key
        request = urllib.request.Request(self.url, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=30) as response:
                result = json.load(response)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise AnkiUnavailableError(f"Cannot reach AnkiConnect at {self.url}: {error}. "
                                     "Open Anki with AnkiConnect enabled. No request will be retried automatically.") from error
        if not isinstance(result, dict) or "error" not in result or "result" not in result:
            raise ImportErrorMessage("Unexpected AnkiConnect response.")
        if result["error"] is not None:
            raise ImportErrorMessage(f"AnkiConnect {action}: {result['error']}")
        return result["result"]


def check_anki(client, config):
    profile = client.call("getActiveProfile")
    if profile != config["profile"]:
        raise ImportErrorMessage(f"Active Anki profile is {profile!r}; open {config['profile']!r} and retry.")
    fields = client.call("modelFieldNames", modelName=config["model_name"])
    validate_fields(config, fields)
    if config["deck_name"] not in client.call("deckNames"):
        raise ImportErrorMessage(f"Deck {config['deck_name']!r} does not exist. Create it in Anki first.")


def recover_attempts(client, config, history):
    """Query only tags from uncertain previous writes; never enumerate notes."""
    escaped = config["model_name"].replace("\\", "\\\\").replace('"', '\\"')
    for tag, word, timestamp in history.pending_attempts():
        if client.call("getActiveProfile") != config["profile"]:
            raise ImportErrorMessage("Anki profile changed during recovery.")
        ids = client.call("findNotes", query=f'note:"{escaped}" tag:{tag}')
        if not isinstance(ids, list) or any(not isinstance(nid, int) for nid in ids):
            raise ImportErrorMessage("Anki returned invalid note IDs during recovery.")
        if len(ids) > 1:
            raise ImportErrorMessage(f"Multiple notes match the pending import of {word}; inspect tag:{tag} in Anki.")
        if ids:
            history.record_values(tag, word, timestamp, ids[0])
            print(f"Recovered {word} (note {ids[0]})", file=sys.stderr)
        else:
            history.clear_attempt(tag)


def load_definitions(path):
    if not path:
        return {}
    values = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(values, dict) or any(not isinstance(v, str) for v in values.values()):
        raise ImportErrorMessage("Definitions must be a JSON object mapping written words to plain-text definitions.")
    return {word_key(k): v for k, v in values.items()}


def text_html(text):
    return html.escape(text.strip()).replace("\n", "<br>")


def definition_text(value):
    return value.text if isinstance(value, Definition) else value


def resolve_definitions(entries, supplied, skip_online_dict=False):
    # Values are held only for this selection, to display/import them. No files
    # are written, and neither successful nor failed lookups are cached.
    definitions = dict(supplied)
    errors = {}
    if skip_online_dict:
        return definitions, errors
    provider = Kotobank()
    for i, entry in enumerate(entries, 1):
        if definition_text(definitions.get(entry.key, "")).strip():
            continue
        print(f"Daijisen {i}/{len(entries)}: {entry.key}", file=sys.stderr, flush=True)
        reading = re.search(r"\[([^]]+)\]$", entry.expression)
        try:
            definition = provider.lookup(entry.key, reading[1] if reading else "")
        except DictionaryUnavailable as error:
            errors[entry.key] = str(error)
            print(f"Dictionary lookup stopped: {error}", file=sys.stderr)
            break
        if definition:
            definitions[entry.key] = definition
        else:
            errors[entry.key] = "No matching Digital Daijisen entry found."
            print(f"No Digital Daijisen entry: {entry.key}", file=sys.stderr)
    return definitions, errors


def make_note(entry, config, definitions):
    date = datetime.fromtimestamp(entry.timestamp / 1000).astimezone().isoformat(timespec="seconds")
    source = "\n".join(filter(None, [entry.title, entry.authors, date,
                                      f"Kindle lookup: {entry.word}"]))
    definition = definitions.get(entry.key, "")
    source_html = text_html(source)
    if isinstance(definition, Definition):
        source_html += '<br><a href="' + html.escape(definition.source_url, quote=True) + '">デジタル大辞泉（コトバンク）</a>'
    return {
        "deckName": config["deck_name"],
        "modelName": config["model_name"],
        "fields": {
            config["word_field"]: text_html(entry.expression),
            config["definition_field"]: text_html(definition_text(definition)),
            config["sentence_field"]: text_html(entry.sentence),
            config["source_field"]: source_html,
        },
        "options": {"allowDuplicate": True},
        "tags": ["kindle_import", lookup_tag(entry)],
    }


def import_entries(client, entries, config, definitions, history):
    """Add individually: retain successes, report failures, make retries idempotent."""
    added, failed = [], []
    for entry in entries:
        if history.contains(entry):
            continue
        # Refuse to continue if the user switched profiles while this command ran.
        if client.call("getActiveProfile") != config["profile"]:
            raise ImportErrorMessage("Anki profile changed during import. Stop here; rerun after selecting the target profile.")
        history.mark_attempt(entry)
        try:
            nid = client.call("addNote", note=make_note(entry, config, definitions))
            if not isinstance(nid, int) or isinstance(nid, bool):
                raise ImportErrorMessage("Anki did not return a note ID.")
            history.record(entry, nid)
            added.append(nid)
            print(f"Added {entry.expression} (note {nid})", flush=True)
        except ImportErrorMessage as error:
            failed.append(entry.key)
            print(f"Failed {entry.expression}: {error}", file=sys.stderr, flush=True)
            # Pending attempts survive failures. On the next run only their
            # exact import tags are checked, to recognize any committed note.
            if isinstance(error, AnkiUnavailableError):
                break
    print(f"Imported {len(added)}; failed {len(failed)}; unattempted {len(entries) - len(added) - len(failed)}.")
    return 1 if failed else 0


def parse_time(value):
    try:
        return int(datetime.fromisoformat(value).timestamp() * 1000)
    except (ValueError, OverflowError) as error:
        raise argparse.ArgumentTypeError("Use an ISO date or datetime, e.g. 2026-06-05 or 2026-06-04T23:54:42+09:00.") from error


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("command", choices=["books", "preview", "doctor", "import", "history", "seed-history"])
    cli.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    cli.add_argument("--kindle", help="Kindle storage root or vocab.db (default: detect connected device)")
    cli.add_argument("--book", help="Filter by a case-insensitive substring of the book title")
    cli.add_argument("--history", type=Path, default=DEFAULT_HISTORY, help="Local import history (default: beside this script)")
    cli.add_argument("--before", type=parse_time, help="For seed-history only: mark lookups strictly before this ISO date/time as already imported")
    cli.add_argument("--definitions", help="Optional JSON mapping words to plain-text definitions")
    cli.add_argument("--skip-online-dict", action="store_true", help="Skip online dictionary lookups (useful for a quick preview)")
    cli.add_argument("--allow-missing-definitions", action="store_true", help="Allow importing notes with a blank Description")
    cli.add_argument("--yes", action="store_true", help="Import the displayed selection without the interactive confirmation")
    cli.add_argument("--json", action="store_true", help="Machine-readable preview or book list")
    return cli


def run_command(args, config, history):
    if args.command == "history":
        print(json.dumps({"history": str(history.path), "profile": config["profile"], **history.stats()},
                         ensure_ascii=False, indent=2))
        return 0
    if args.command == "doctor":
        client = AnkiConnect(config["anki_url"])
        check_anki(client, config)
        print(f"AnkiConnect OK: profile={config['profile']}, model={config['model_name']}, deck={config['deck_name']}")
        database = find_database(args.kindle)
        entries = read_entries(database, config["language"])
        print(f"Kindle OK: {database}\n{len(entries)} lookups; {len({e.key for e in entries})} dictionary headwords.")
        print(f"History OK: {history.path} ({history.stats()})")
        return 0
    database = find_database(args.kindle)
    entries = read_entries(database, config["language"])
    if args.command == "seed-history":
        count = history.seed(entries, args.before)
        print(f"Recorded {count} legacy lookups as already imported. History: {history.path}")
        print(json.dumps(history.stats(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "books":
        books = {}
        for e in entries:
            books.setdefault(e.title, set()).add(e.key)
        rows = [{"title": title, "words": len(words)} for title, words in sorted(books.items())]
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            for row in rows:
                print(f"{row['words']:5}  {row['title']}")
        return 0
    definitions = load_definitions(args.definitions)
    if args.command == "import":
        client = AnkiConnect(config["anki_url"])
        check_anki(client, config)
        recover_attempts(client, config, history)
    selected, counts = select_entries(entries, history, args.book)
    if counts.get("awaiting_confirmation"):
        print("Unconfirmed previous imports exist; run import to resolve them by their Anki tags.", file=sys.stderr)
    definitions, lookup_errors = resolve_definitions(selected, definitions, args.skip_online_dict)
    if args.json:
        if args.command != "preview":
            raise ImportErrorMessage("--json is supported for books and preview only.")
        print(json.dumps({"database": str(database), "profile": config["profile"], "history": str(history.path), "counts": counts,
                          "entries": [dict(asdict(e), expression=e.expression,
                                           definition=definition_text(definitions.get(e.key, "")),
                                           definition_url=(definitions[e.key].source_url
                                                           if isinstance(definitions.get(e.key), Definition) else None),
                                           lookup_error=lookup_errors.get(e.key)) for e in selected]},
                         ensure_ascii=False, indent=2))
    else:
        print(f"Source: {database}\nTarget: {config['profile']} / {config['deck_name']} / {config['model_name']}")
        print(f"History: {history.path}")
        print("; ".join(f"{key}: {value}" for key, value in counts.items()))
        for e in selected:
            print(f"\n{e.expression} — {e.title}\n  {e.sentence.strip()}")
            value = definitions.get(e.key, "")
            if definition_text(value):
                print("  Definition: " + definition_text(value).replace("\n", "\n  "))
            if isinstance(value, Definition):
                print("  " + value.source_url)
        missing = sum(not definition_text(definitions.get(e.key, "")).strip() for e in selected)
        print(f"\n{missing}/{len(selected)} selected words have no definition.")
    if args.command == "preview" or not selected:
        return int(bool(lookup_errors or counts.get("awaiting_confirmation")))
    missing_entries = [e for e in selected if not definition_text(definitions.get(e.key, "")).strip()]
    if missing_entries and not args.allow_missing_definitions:
        print("Skipping words without definitions: " + ", ".join(e.key for e in missing_entries), file=sys.stderr)
        print("Supply --definitions FILE for these words, or explicitly use --allow-missing-definitions.", file=sys.stderr)
        selected = [e for e in selected if e not in missing_entries]
        if not selected:
            return 1
    if not args.yes:
        if not sys.stdin.isatty():
            raise ImportErrorMessage("Import requires confirmation. Run in a terminal, or use --yes after reviewing preview.")
        if input(f"\nImport {len(selected)} words into {config['profile']}? [y/N] ").strip().lower() != "y":
            print("Cancelled.")
            return 0
    check_anki(client, config)
    selected = [e for e in selected if not history.contains(e)]
    result = import_entries(client, selected, config, definitions, history)
    return result or int(bool(missing_entries and not args.allow_missing_definitions))


def main(argv=None):
    cli = parser()
    args = cli.parse_args(argv)
    if args.command == "seed-history" and args.before is None:
        cli.error("seed-history requires --before DATE")
    if args.command != "seed-history" and args.before is not None:
        cli.error("--before is only supported with seed-history")
    if args.json and args.command not in {"books", "preview", "history"}:
        cli.error("--json is supported for books, preview, and history only")
    try:
        config = load_config(args.config)
        with ImportHistory(args.history, config) as history:
            lock = history.exclusive() if args.command in {"import", "seed-history"} else nullcontext()
            with lock:
                return run_command(args, config, history)
    except (ImportErrorMessage, OSError, sqlite3.Error, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("Stopped. Rerun safely; successful imports are saved and uncertain writes will be checked.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
