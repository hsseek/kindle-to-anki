# Kindle → Anki CLI

Import Japanese Vocabulary Builder words from a USB-connected Kindle, `Kindle` deck, and existing `Japanese-words` note type.
Requires Python 3.10+ on Linux, an internet connection for definitions, and the
already-installed AnkiConnect add-on. No Python packages need to be installed.

## Everyday use

Connect and unlock the Kindle, then open its storage in your Linux file manager
to make its MTP/GVfs mount available. For importing, open Anki in the **sun** profile.

```bash
python3 kindle_anki.py doctor
python3 kindle_anki.py preview
python3 kindle_anki.py import
```

- `doctor` checks AnkiConnect, the active profile, deck, note fields, Kindle
  database, and local history. It does not fetch dictionary definitions.
- `preview` shows every pending word, its original sentence, and an online
  Digital Daijisen definition. It never adds notes or marks words imported.
  Anki can be closed: previews use local import history, not the Anki collection.
- `import` fetches definitions, displays all pending words, and asks for
  confirmation before adding notes. Each successful import is saved immediately.

There is **no batch limit or ongoing date cutoff**. `--limit`, `--all`, and
`--since` have been removed. Plain `import` processes all pending words.

For a fast preview without dictionary requests:

```bash
python3 kindle_anki.py preview --skip-online-dict
python3 kindle_anki.py preview --skip-online-dict --json
```

Filter to a book using a case-insensitive substring of its title:

```bash
python3 kindle_anki.py preview --book 'もものかんづめ'
python3 kindle_anki.py import --book 'もものかんづめ'
```

`books` lists all books with recorded vocabulary, including completed words.
Append `--yes` to `import` to skip its confirmation after reviewing the selection.
Large imports take time because fresh dictionary requests are spaced at least
one second apart. Previewing and then importing repeats those requests.

## Local import history

`import-history.sqlite3` beside the script records completed Kindle lookup IDs,
normalized words, original lookup timestamps, Anki note IDs when available, and
unfinished import attempts. It contains no dictionary definitions, original
sentences, or downloaded pages. It is excluded from Git.

The current history has been seeded with Kindle lookups **strictly before
2026-06-28 at midnight Korea time**, because those words were already imported.
Seeding records those individual words/lookups; it does not impose a date filter
on future runs. A new word with an older timestamp can still be imported if it
is absent from the history. June 28 itself was not included in the seed.

```bash
python3 kindle_anki.py history
```

History checks use local SQLite indexes. Normal imports do not scan or download
Anki notes. Duplicate prevention covers **Kindle import history only**: words
added manually to Anki do not block imports. A normalized word already in the
history is skipped even if you look it up again in another book. Multiple pending
lookups of a word produce one note using its first complete sentence.

Keep this history file and include it in backups. Deleting an Anki note does not
clear its history entry or cause automatic reimport. History is separated by
Anki endpoint, profile, note type, and word field; changing one starts a separate
history target inside the same file.

For explicit legacy migration or rebuilding an absent history from a known
previous import boundary, use:

```bash
python3 kindle_anki.py seed-history --before '2026-06-28T00:00:00+09:00'
```

This marks every recorded Kindle word before that instant as already imported,
without adding or checking Anki notes. It is safe to repeat and is not needed
for everyday imports. Use it only for words you know were previously imported.
You can select another history file with `--history /path/to/history.sqlite3`.

## Interrupted imports

Each add request is recorded as unfinished before being sent to Anki. After Anki
returns a note ID, the history records success immediately. Failed definitions
and failed adds remain eligible for later retries.

If Anki accepted a note but its reply was lost, or the script stopped before
saving the success, the next `import` checks only that unfinished lookup's unique
Anki tag. It records an existing note as completed, or retries if none exists.
It never retrieves your entire collection. Previews report unfinished attempts
and leave their resolution to `import`. Simultaneous imports using the same
history file are prevented.

## Fields and definitions

| Content | Existing field |
| --- | --- |
| Dictionary word with recorded reading when available | `Word` |
| Japanese definition from Digital Daijisen | `Description` |
| Original lookup sentence | `Additional` |
| Book title, author, lookup date, selected word, dictionary entry link | `References` |

New notes go into `Kindle` using your existing note templates. `TTS` and `Line`
remain available for your usual workflow. Kindle files are never modified; its
vocabulary database is copied to temporary local storage before SQLite reads.

The Kindle database does not store definition text. Definitions come from
[Kotobank](https://kotobank.jp/), specifically **デジタル大辞泉**, with no fallback
to other dictionaries. Recorded readings help select among homographs where
available. Lookups are **not cached**: each command fetches fresh definitions,
held in memory only for the current selection. Completed words are excluded
before any dictionary request.

Missing definitions are reported and skipped. Other words can still be imported;
a partial import exits with code 1. A network/service error stops further lookup
requests. To intentionally import blank definitions, use
`--allow-missing-definitions`; combine it with `--skip-online-dict` to skip online lookups.

You can also supply plain-text definitions from an existing export:

```json
{
  "芸人": "Your definition here",
  "封筒": "Your definition here"
}
```

```bash
python3 kindle_anki.py preview --definitions definitions.json
python3 kindle_anki.py import --definitions definitions.json
```

Supplied definitions take precedence over online requests. This optional input
is not an automatically created cache. Text is escaped as HTML; newlines become
line breaks. Kotobank's page layout can change; unrecognized definition sections
are reported as lookup failures.

## Configuration and saved databases

Change profile/deck/model/field mappings in `config.json`. Keep AnkiConnect's
local URL unless intentionally using another endpoint. If it requires an API
key, set `ANKICONNECT_API_KEY` in the environment.

The connected device is detected each time. To deliberately read a saved database:

```bash
python3 kindle_anki.py preview --kindle /path/to/vocab.db --skip-online-dict
```

Old `~/.kindle/` copies are not used automatically.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Tests use a fake Anki service and synthetic dictionary pages. They cover history
seeding, word/lookup deduplication, imports without collection scans, persistence,
failed writes, lost replies, profile isolation, concurrent imports, missing
definitions, dictionary source selection, and fresh requests without caching.
They never add notes to the real Anki collection.
