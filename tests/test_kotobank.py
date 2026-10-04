from email.message import Message
import io
import unittest
from unittest.mock import patch
import urllib.error

from kotobank import DictionaryUnavailable, Kotobank, exact_entry_links, extract_definition


# Synthetic definitions: fixtures model the article/h2/h3/description structure,
# rather than copying published dictionary entries into the repository.
PAGE = """
<html><body><h1>コトバンク 芸人（読み）ゲイニン</h1>
<article><h2><a>デジタル大辞泉</a> 「芸人」の意味・読み・例文・類語</h2>
<section class="description"><h3>げい‐にん【芸人】</h3>
<p>１ テスト用の<a href="/word/説明-1">説明</a>。<br>２ 次の意味。</p>
<p>［類語］無関係な一覧</p></section></article>
<article><h2>精選版 日本国語大辞典</h2><section class="description">
<h3>げい‐にん【芸人】</h3><p>別の辞書の本文。</p></section></article>
<article><h2>デジタル大辞泉プラス</h2><section class="description">
<h3>げい‐にん【芸人】</h3><p>Plusの本文。</p></section></article>
</body></html>
"""


class KotobankTests(unittest.TestCase):
    def test_only_daijisen_with_line_breaks_and_no_synonyms(self):
        body = extract_definition(PAGE, "芸人", "げいにん")
        self.assertIn("１ テスト用の説明。\n２ 次の意味。", body)
        self.assertNotIn("別の辞書", body)
        self.assertNotIn("Plus", body)
        self.assertNotIn("無関係", body)

    def test_wrong_headword_rejected(self):
        self.assertIsNone(extract_definition(PAGE, "芸能人"))

    def test_kana_headword_with_dictionary_separators(self):
        page = '<html>コトバンク<article><h2>デジタル大辞泉</h2><section class="description"><h3>ぐしゃ‐ぐしゃ</h3><p>テスト用の意味</p></section></article></html>'
        self.assertIn("テスト用の意味", extract_definition(page, "ぐしゃぐしゃ"))

    def test_no_other_dictionary_fallback(self):
        other = '<html>コトバンク<article><h2>別の辞書</h2><section class="description"><h3>芸人</h3>説明</section></article></html>'
        self.assertIsNone(extract_definition(other, "芸人"))

    def test_select_recorded_reading_for_homographs(self):
        page = '''<html>コトバンク<article><h2>デジタル大辞泉</h2>
        <section class="description"><h3>なま【生】</h3>第一の説明</section>
        <section class="description"><h3>せい【生】</h3>第二の説明</section>
        </article></html>'''
        self.assertIn("第二の説明", extract_definition(page, "生", "セイ"))
        self.assertNotIn("第一の説明", extract_definition(page, "生", "せい"))

    def test_changed_layout_and_challenge_fail_closed(self):
        page = '<html>コトバンク<article><h2>デジタル大辞泉</h2><h3>芸人</h3><div class="new-layout">本文</div></article></html>'
        with self.assertRaisesRegex(DictionaryUnavailable, "layout"):
            extract_definition(page, "芸人")
        with self.assertRaisesRegex(DictionaryUnavailable, "recognized"):
            extract_definition("<html>Please verify you are human</html>", "芸人")

    def test_search_links_exact_only(self):
        page = '''<a href="/word/%E8%8A%B8%E4%BA%BA-59100">芸人</a>
        <a href="/word/芸人-59100">芸人</a><a href="/word/芸能人-2">芸能人</a>
        <a href="https://example.com/word/芸人-1">外部</a>'''
        links = exact_entry_links(page, "芸人")
        self.assertTrue(links)
        self.assertTrue(all("59100" in url for url in links))

    def test_direct_lookup_and_no_result_cache(self):
        provider = Kotobank(delay=0)
        with patch.object(provider, "fetch", return_value=(PAGE, "https://kotobank.jp/word/test-1")) as fetched:
            first = provider.lookup("芸人", "げいにん")
            second = provider.lookup("芸人", "げいにん")
        self.assertEqual(first, second)
        self.assertEqual(fetched.call_count, 2)

    def test_search_fallback_follows_exact_entry(self):
        provider = Kotobank(delay=0)
        search = '<html>コトバンク<a href="/word/%E8%8A%B8%E4%BA%BA-59100">芸人</a></html>'
        with patch.object(provider, "fetch", side_effect=[("", "direct"), (search, "search"), (PAGE, "https://kotobank.jp/word/test-1")]) as fetched:
            definition = provider.lookup("芸人")
        self.assertIsNotNone(definition)
        self.assertEqual(fetched.call_count, 3)

    def test_missing_entry_returns_none(self):
        provider = Kotobank(delay=0)
        with patch.object(provider, "fetch", return_value=("<html>コトバンク 見つかりません</html>", "https://kotobank.jp/search")):
            self.assertIsNone(provider.lookup("存在しないテスト語"))

    def test_fresh_requests_send_no_cache_headers(self):
        provider = Kotobank(delay=0)
        class Response(io.BytesIO):
            headers = Message()
            def geturl(self):
                return "https://kotobank.jp/word/test-1"
        responses = [Response(PAGE.encode()), Response(PAGE.encode())]
        with patch.object(provider.opener, "open", side_effect=responses) as opened:
            provider.lookup("芸人")
            provider.lookup("芸人")
        self.assertEqual(opened.call_count, 2)
        for call in opened.call_args_list:
            self.assertEqual(call.args[0].get_header("Cache-control"), "no-cache, no-store")

    def test_rate_limit_error_is_not_retried(self):
        provider = Kotobank(delay=0)
        error = urllib.error.HTTPError("https://kotobank.jp", 429, "Too many requests", {}, None)
        with patch.object(provider.opener, "open", side_effect=error) as opened:
            with self.assertRaisesRegex(DictionaryUnavailable, "429"):
                provider.lookup("芸人")
        opened.assert_called_once()


if __name__ == "__main__":
    unittest.main()
