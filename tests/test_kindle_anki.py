import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import kindle_anki as app


def entry(word="芸人", stem="芸人【げい‐にん】", **kwargs):
    values = dict(lookup_id=f"lookup-{word}", word=word, stem=stem,
                  sentence="営業課の私の役割は、芸人としての営業に終わった。",
                  title="もものかんづめ", authors="さくらももこ",
                  timestamp=1789000000000, category=0, book_id="book-1")
    values.update(kwargs)
    return app.Entry(**values)


class FakeAnki:
    def __init__(self):
        self.profile = "sun"
        self.existing = ["裏腹[うらはら]"]
        self.added = []
        self.calls = []
        self.fail_words = set()

    def call(self, action, **params):
        self.calls.append((action, params))
        if action == "getActiveProfile":
            return self.profile
        if action == "modelFieldNames":
            return ["Word", "Description", "Additional", "TTS", "References", "Line"]
        if action == "deckNames":
            return ["Kindle"]
        if action == "findNotes":
            tag = params["query"].split("tag:", 1)[1]
            return [1001 + i for i, note in enumerate(self.added) if tag in note["tags"]]
        if action == "notesInfo":
            raise AssertionError("The importer must not enumerate Anki notes")
        if action == "addNote":
            note = params["note"]
            word = note["fields"]["Word"]
            if app.word_key(word) in self.fail_words:
                raise app.ImportErrorMessage("Deliberate note failure")
            if not note["options"].get("allowDuplicate") and app.word_key(word) in {app.word_key(w) for w in self.existing}:
                raise app.ImportErrorMessage("Duplicate")
            self.added.append(note)
            self.existing.append(word)
            return 1000 + len(self.added)
        raise AssertionError(action)


class ImporterTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.history_path = Path(directory.name) / "history.sqlite3"
        default = patch.object(app, "DEFAULT_HISTORY", self.history_path)
        default.start()
        self.addCleanup(default.stop)
        self.history = app.ImportHistory(self.history_path, app.DEFAULTS)
        self.addCleanup(self.history.__exit__, None, None, None)

    def test_daijisen_headwords_and_readings(self):
        cases = [
            ("桜吹‐雪【さくら‐ふぶき】", "桜吹雪[さくらふぶき]"),
            ("辞める／▽罷める【や・める】", "辞める[やめる]"),
            ("くし【串】", "串[くし]"),
            ("×喋る【しゃべ・る】", "喋る[しゃべる]"),
            ("あやうい【危うい】", "危うい[あやうい]"),
            ("ねだる", "ねだる"),
            ("打ち明ける", "打ち明ける"),
        ]
        for stem, expected in cases:
            with self.subTest(stem=stem):
                self.assertEqual(app.dictionary_expression(stem, "ねだる"), expected)
        self.assertEqual(app.dictionary_expression("打ち明ける", "打ち明けない"), "打ち明ける")
        self.assertEqual(app.dictionary_expression("ねだる", "ねだった"), "ねだる")
        self.assertEqual(app.dictionary_expression("しょ‐せい", "書生"), "書生")

    def test_existing_furigana_and_ruby_are_recognized(self):
        for text in ["悔[く]いる", "<b>悔</b>[く]いる", "<ruby>悔<rt>く</rt></ruby>いる"]:
            self.assertEqual(app.word_key(text), "悔いる")

    def test_select_history_word_keys_and_repeated_lookups(self):
        first = entry(timestamp=1000)
        second = entry(timestamp=2000, lookup_id="later")
        known = entry("裏腹", "裏腹【うらはら】", timestamp=3000)
        incomplete = entry("枕", "枕【まくら】", sentence="", timestamp=4000)
        other = entry("封筒", "封筒【ふうとう】", timestamp=5000)
        self.history.record(known, 123)
        selected, counts = app.select_entries([first, second, known, incomplete, other], self.history)
        self.assertEqual(selected, [first, other])
        self.assertEqual(counts["new_words"], 2)
        self.assertEqual(counts["already_in_history"], 1)
        self.assertEqual(counts["repeated_lookup"], 1)
        self.assertEqual(counts["incomplete"], 1)
        same_word_elsewhere = entry("裏腹", "裏腹【うらはら】", lookup_id="other", book_id="other-book")
        self.assertEqual(app.select_entries([same_word_elsewhere], self.history)[0], [])

    def test_repeated_lookup_one_note(self):
        selected, counts = app.select_entries([entry(), entry(lookup_id="second")], self.history)
        self.assertEqual(len(selected), 1)
        self.assertEqual(counts["repeated_lookup"], 1)

    def test_html_safety_and_preserved_fields(self):
        note = app.make_note(entry(sentence="A < B & C", title="<script>bad</script>"), app.DEFAULTS,
                             {"芸人": "意味 <text>\n次の行"})
        self.assertEqual(note["fields"]["Additional"], "A &lt; B &amp; C")
        self.assertEqual(note["fields"]["Description"], "意味 &lt;text&gt;<br>次の行")
        self.assertIn("&lt;script&gt;", note["fields"]["References"])
        self.assertNotIn("TTS", note["fields"])
        self.assertTrue(note["options"]["allowDuplicate"])

    def test_wrong_profile_blocks_writes(self):
        client = FakeAnki()
        client.profile = "User 1"
        with self.assertRaisesRegex(app.ImportErrorMessage, "User 1"):
            app.check_anki(client, app.DEFAULTS)
        self.assertFalse(client.added)

    def test_partial_failure_retry_only_missing_word(self):
        client = FakeAnki()
        client.fail_words = {"封筒"}
        entries = [entry(), entry("封筒", "封筒【ふうとう】")]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(app.import_entries(client, entries, app.DEFAULTS, {}, self.history), 1)
            app.recover_attempts(client, app.DEFAULTS, self.history)
            pending, _ = app.select_entries(entries, self.history)
            self.assertEqual([e.key for e in pending], ["封筒"])
            client.fail_words.clear()
            self.assertEqual(app.import_entries(client, pending, app.DEFAULTS, {}, self.history), 0)
        self.assertEqual(len(client.added), 2)

    def test_profile_switch_mid_import_stops(self):
        client = FakeAnki()
        original = client.call
        def call(action, **params):
            if action == "getActiveProfile" and client.added:
                return "User 1"
            return original(action, **params)
        client.call = call
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(app.ImportErrorMessage, "profile changed"):
                app.import_entries(client, [entry(), entry("封筒", "封筒【ふうとう】")], app.DEFAULTS, {}, self.history)
        self.assertEqual(len(client.added), 1)

    def test_uncertain_network_failure_stops_without_retry(self):
        client = FakeAnki()
        original = client.call
        def call(action, **params):
            if action == "addNote":
                client.calls.append((action, params))
                raise app.AnkiUnavailableError("Cannot reach AnkiConnect: timeout")
            return original(action, **params)
        client.call = call
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = app.import_entries(client, [entry(), entry("封筒", "封筒【ふうとう】")], app.DEFAULTS, {}, self.history)
        self.assertEqual(result, 1)
        self.assertEqual(sum(a == "addNote" for a, _ in client.calls), 1)

    def test_transport_wire_payload(self):
        client = app.AnkiConnect("http://127.0.0.1:8765")
        class Response(io.BytesIO):
            pass
        with patch.object(client.opener, "open", return_value=Response(b'{"error":null,"result":"sun"}')) as opened:
            self.assertEqual(client.call("getActiveProfile"), "sun")
        request = opened.call_args.args[0]
        self.assertEqual(json.loads(request.data), {"action": "getActiveProfile", "version": 6, "params": {}})

    def test_cli_import_idempotent_and_definition_gate(self):
        client = FakeAnki()
        with patch.object(app, "AnkiConnect", return_value=client), \
             patch.object(app, "find_database", return_value=Path("fixture.db")), \
             patch.object(app, "read_entries", return_value=[entry()]), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(app.main(["import", "--yes", "--skip-online-dict"]), 1)
            self.assertFalse(client.added)
            self.assertEqual(app.main(["import", "--yes", "--skip-online-dict", "--allow-missing-definitions"]), 0)
            self.assertEqual(app.main(["import", "--yes", "--skip-online-dict", "--allow-missing-definitions"]), 0)
        self.assertEqual(len(client.added), 1)

    def test_cli_preview_never_calls_ankiconnect(self):
        with patch.object(app, "AnkiConnect") as client, \
             patch.object(app, "find_database", return_value=Path("fixture.db")), \
             patch.object(app, "read_entries", return_value=[entry()]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(app.main(["preview", "--json", "--skip-online-dict"]), 0)
        client.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["entries"][0]["expression"], "芸人[げいにん]")

    def test_database_copy_preserves_source_and_joins_book(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vocab.db"
            db = sqlite3.connect(path)
            db.executescript("""
                CREATE TABLE WORDS(id TEXT, word TEXT, stem TEXT, lang TEXT, category INTEGER);
                CREATE TABLE LOOKUPS(id TEXT, word_key TEXT, usage TEXT, timestamp INTEGER, book_key TEXT);
                CREATE TABLE BOOK_INFO(id TEXT, title TEXT, authors TEXT);
                INSERT INTO WORDS VALUES('ja:芸人', '芸人', '芸人【げいにん】', 'ja', 0);
                INSERT INTO LOOKUPS VALUES('lookup', 'ja:芸人', '芸人としての営業', 1000, 'book');
                INSERT INTO BOOK_INFO VALUES('book', 'もものかんづめ', 'さくらももこ');
            """)
            db.close()
            before = path.read_bytes()
            result = app.read_entries(path, "ja")
            self.assertEqual(result[0].title, "もものかんづめ")
            self.assertEqual(result[0].expression, "芸人[げいにん]")
            self.assertEqual(path.read_bytes(), before)

    def test_detection_no_stale_fallback_and_ambiguous_devices(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            with patch.dict(app.os.environ, {"XDG_RUNTIME_DIR": str(base)}):
                with patch.object(Path, "home", return_value=base):
                    with self.assertRaisesRegex(app.ImportErrorMessage, "No mounted Kindle"):
                        app.find_database()
                    db = base / "gvfs/mtp:host=Amazon_Kindle_1/Internal Storage/system/vocabulary/vocab.db"
                    db.parent.mkdir(parents=True)
                    db.touch()
                    self.assertEqual(app.find_database(), db)
                    second = base / "gvfs/mtp:host=Amazon_Kindle_2/Internal Storage/system/vocabulary/vocab.db"
                    second.parent.mkdir(parents=True)
                    second.touch()
                    with self.assertRaisesRegex(app.ImportErrorMessage, "Multiple Kindles"):
                        app.find_database()

    def test_definition_json_and_config_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "definitions.json"
            path.write_text('{"芸人": "意味"}', encoding="utf-8")
            self.assertEqual(app.load_definitions(path), {"芸人": "意味"})
            path.write_text('{"word_field":"Description"}')
            with self.assertRaisesRegex(app.ImportErrorMessage, "different Anki field"):
                app.load_config(path)

    def test_online_definition_import_and_attribution(self):
        client = FakeAnki()
        definition = app.Definition("テスト用の意味", "https://kotobank.jp/word/test-123")
        with patch.object(app, "AnkiConnect", return_value=client), \
             patch.object(app, "find_database", return_value=Path("fixture.db")), \
             patch.object(app, "read_entries", return_value=[entry()]), \
             patch.object(app, "Kotobank") as provider, \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            provider.return_value.lookup.return_value = definition
            self.assertEqual(app.main(["import", "--yes"]), 0)
            provider.return_value.lookup.assert_called_once_with("芸人", "げいにん")
            self.assertEqual(app.main(["import", "--yes"]), 0)
            # Already imported words are excluded before requesting definitions.
            provider.return_value.lookup.assert_called_once()
        self.assertEqual(client.added[0]["fields"]["Description"], "テスト用の意味")
        self.assertIn('<a href="https://kotobank.jp/word/test-123">', client.added[0]["fields"]["References"])

    def test_missing_online_entry_skipped_while_known_entry_imported(self):
        client = FakeAnki()
        with patch.object(app, "AnkiConnect", return_value=client), \
             patch.object(app, "find_database", return_value=Path("fixture.db")), \
             patch.object(app, "read_entries", return_value=[entry(), entry("封筒", "封筒【ふうとう】")]), \
             patch.object(app, "Kotobank") as provider, \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            provider.return_value.lookup.side_effect = [app.Definition("テスト", "https://kotobank.jp/word/test-1"), None]
            self.assertEqual(app.main(["import", "--yes"]), 1)
        self.assertEqual(len(client.added), 1)
        self.assertEqual(app.word_key(client.added[0]["fields"]["Word"]), "芸人")

    def test_dictionary_outage_stops_further_lookups(self):
        with patch.object(app, "Kotobank") as provider, contextlib.redirect_stderr(io.StringIO()):
            provider.return_value.lookup.side_effect = app.DictionaryUnavailable("HTTP 429")
            definitions, errors = app.resolve_definitions([entry(), entry("封筒", "封筒【ふうとう】")], {})
        self.assertFalse(definitions)
        self.assertIn("HTTP 429", errors["芸人"])
        provider.return_value.lookup.assert_called_once()

    def test_each_preview_fetches_fresh_definitions(self):
        with patch.object(app, "find_database", return_value=Path("fixture.db")), \
             patch.object(app, "read_entries", return_value=[entry()]), \
             patch.object(app, "Kotobank") as provider, \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            provider.return_value.lookup.return_value = app.Definition("テスト", "https://kotobank.jp/word/test-1")
            self.assertEqual(app.main(["preview"]), 0)
            self.assertEqual(app.main(["preview"]), 0)
        self.assertEqual(provider.return_value.lookup.call_count, 2)

    def test_seed_history_and_no_runtime_cutoff(self):
        cutoff = app.parse_time("2026-06-28T00:00:00+09:00")
        older = entry("封筒", "封筒【ふうとう】", timestamp=cutoff - 1)
        boundary = entry(timestamp=cutoff)
        unseen_old = entry("枕", "枕【まくら】", timestamp=cutoff - 10000)
        config = dict(app.DEFAULTS)
        with patch.object(app, "load_config", return_value=config), \
             patch.object(app, "find_database", return_value=Path("fixture.db")), \
             patch.object(app, "read_entries", return_value=[older, boundary]), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(app.main(["seed-history", "--before", "2026-06-28T00:00:00+09:00"]), 0)
            self.assertEqual(app.main(["seed-history", "--before", "2026-06-28T00:00:00+09:00"]), 0)
        self.assertEqual(self.history.stats()["seeded_lookups"], 1)
        self.assertTrue(self.history.contains(older))
        self.assertFalse(self.history.contains(boundary))
        later_same_word = entry("封筒", "封筒【ふうとう】", timestamp=cutoff + 1000, lookup_id="later")
        selected, _ = app.select_entries([older, boundary, later_same_word, unseen_old], self.history)
        self.assertEqual(selected, [boundary, unseen_old])

    def test_plain_import_selects_all_without_account_scan_or_manual_duplicates(self):
        client = FakeAnki()
        entries = [entry(word=f"単語{i}", stem="", lookup_id=f"lookup{i}") for i in range(25)]
        client.existing = [e.expression for e in entries]  # Manually added notes must not block imports.
        with patch.object(app, "AnkiConnect", return_value=client), \
             patch.object(app, "find_database", return_value=Path("fixture.db")), \
             patch.object(app, "read_entries", return_value=entries), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(app.main(["import", "--yes", "--skip-online-dict", "--allow-missing-definitions"]), 0)
            self.assertEqual(app.main(["import", "--yes", "--skip-online-dict", "--allow-missing-definitions"]), 0)
        self.assertEqual(len(client.added), 25)
        self.assertEqual(self.history.stats()["imported_lookups"], 25)
        self.assertFalse(any(action in {"findNotes", "notesInfo"} for action, _ in client.calls))

    def test_server_commit_with_lost_reply_recovered_without_duplicate(self):
        client = FakeAnki()
        original = client.call
        def call(action, **params):
            result = original(action, **params)
            if action == "addNote":
                raise app.AnkiUnavailableError("Reply lost after Anki committed the note")
            return result
        client.call = call
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(app.import_entries(client, [entry()], app.DEFAULTS, {}, self.history), 1)
            self.assertEqual(self.history.stats()["uncertain_attempts"], 1)
            client.call = original
            app.recover_attempts(client, app.DEFAULTS, self.history)
        self.assertEqual(len(client.added), 1)
        self.assertTrue(self.history.contains(entry()))
        self.assertEqual(self.history.stats()["uncertain_attempts"], 0)
        queries = [params["query"] for action, params in client.calls if action == "findNotes"]
        self.assertEqual(len(queries), 1)
        self.assertIn("tag:kindle_lookup_", queries[0])

    def test_crash_after_server_commit_before_history_save_is_recovered(self):
        client = FakeAnki()
        with patch.object(self.history, "record", side_effect=OSError("disk failure")), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(OSError, "disk failure"):
                app.import_entries(client, [entry()], app.DEFAULTS, {}, self.history)
        self.assertEqual(self.history.stats()["uncertain_attempts"], 1)
        with app.ImportHistory(self.history_path, app.DEFAULTS) as reopened, contextlib.redirect_stderr(io.StringIO()):
            app.recover_attempts(client, app.DEFAULTS, reopened)
            self.assertTrue(reopened.contains(entry()))
        self.assertEqual(len(client.added), 1)

    def test_history_scope_isolated_by_profile_and_model(self):
        self.history.record(entry(), 123)
        with app.ImportHistory(self.history_path, dict(app.DEFAULTS, profile="other")) as other:
            self.assertFalse(other.contains(entry()))
        with app.ImportHistory(self.history_path, dict(app.DEFAULTS, model_name="other")) as other:
            self.assertFalse(other.contains(entry()))

    def test_preview_does_not_change_completion_or_query_uncertain_writes(self):
        self.history.mark_attempt(entry())
        before = self.history.stats()
        with patch.object(app, "AnkiConnect") as client, \
             patch.object(app, "find_database", return_value=Path("fixture.db")), \
             patch.object(app, "read_entries", return_value=[entry()]), \
             contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(app.main(["preview", "--json", "--skip-online-dict"]), 1)
        client.assert_not_called()
        self.assertEqual(self.history.stats(), before)
        self.assertEqual(json.loads(output.getvalue())["counts"]["awaiting_confirmation"], 1)

    def test_simultaneous_import_rejected_before_anki_calls(self):
        with self.history.exclusive(), patch.object(app, "AnkiConnect") as client, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(app.main(["import", "--yes"]), 1)
        client.assert_not_called()



if __name__ == "__main__":
    unittest.main()
