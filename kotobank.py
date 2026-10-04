"""Fetch Digital Daijisen entries from Kotobank, without a lookup cache."""

from dataclasses import dataclass, field
from html.parser import HTMLParser
import re
import time
import urllib.error
import urllib.parse
import urllib.request


class DictionaryUnavailable(Exception):
    """The service could not be reached or its page could not be understood."""


@dataclass(frozen=True)
class Definition:
    text: str
    source_url: str


@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def text(self):
        if self.tag in {"script", "style", "rt", "rp", "nav", "footer"}:
            return ""
        value = "".join(c.text() if isinstance(c, Node) else c for c in self.children)
        if self.tag in {"p", "div", "section", "h1", "h2", "h3", "li", "br"}:
            value = "\n" + value + "\n"
        return value


class PageParser(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]
        self.feed(markup)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def compact(text):
    return re.sub(r"\s+", "", text)


def reading_key(text):
    text = re.sub(r"[\s‐‑–・]", "", text)
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in text)


def heading_matches(heading, word):
    # h3 uses reading【written form】 or reading〖written form〗.
    written = re.search(r"[【〖](.*?)[】〗]", heading)
    if written:
        forms = re.split(r"[／/・]", written[1])
        return any(compact(re.sub(r"[△▽×＝‐]", "", form)) == compact(word) for form in forms)
    plain = re.split(r"[（(]", heading)[0]
    return compact(plain) == compact(word) or reading_key(plain) == reading_key(word)


def extract_definition(markup, word, reading=""):
    root = PageParser(markup).root
    articles = [n for n in root.walk() if n.tag == "article"]
    matches = []
    had_dictionary = False
    for article in articles:
        headings = [n for n in article.walk() if n.tag == "h2"]
        # The edition name must match exactly; Daijisen Plus is a different source.
        if not any(re.match(r"^デジタル大辞泉(?:$|[\s「『の])", n.text().strip()) for n in headings):
            continue
        had_dictionary = True
        descriptions = [n for n in article.walk() if "description" in n.attrs.get("class", "").split()]
        if not descriptions:
            raise DictionaryUnavailable("Kotobank's Daijisen page layout has changed (no definition section).")
        for section in descriptions:
            local_headings = [n for n in section.walk() if n.tag == "h3"]
            if not local_headings:
                local_headings = [n for n in article.walk() if n.tag == "h3"]
            if not any(heading_matches(n.text().strip(), word) for n in local_headings):
                continue
            body = section.text()
            body = re.split(r"[\[［]類語[\]］]", body)[0]
            lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in body.splitlines()]
            body = "\n".join(line for line in lines if line)
            if not body:
                continue
            exact_reading = any(reading and reading_key(re.split(r"[【〖]", n.text())[0]) == reading_key(reading)
                                for n in local_headings)
            matches.append((exact_reading, body))
    if reading and any(exact for exact, _ in matches):
        matches = [(exact, body) for exact, body in matches if exact]
    if matches:
        return "\n\n".join(dict.fromkeys(body for _, body in matches))
    # Search pages and pages with only other dictionaries are legitimate misses.
    if not articles and "/word/" not in markup and "コトバンク" not in root.text():
        raise DictionaryUnavailable("The response is not a recognized Kotobank page.")
    if had_dictionary and not any(n.tag == "h3" for a in articles for n in a.walk()):
        raise DictionaryUnavailable("Kotobank's Daijisen page has no entry heading.")
    return None


def exact_entry_links(markup, word):
    links = []
    for node in PageParser(markup).root.walk():
        if node.tag != "a":
            continue
        url = urllib.parse.urljoin("https://kotobank.jp", node.attrs.get("href", ""))
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "kotobank.jp" or not parsed.path.startswith("/word/"):
            continue
        slug = urllib.parse.unquote(parsed.path[len("/word/"):])
        slug = re.sub(r"-\d+$", "", slug)
        if compact(slug) == compact(word) and url not in links:
            links.append(url)
    return links


class Kotobank:
    """Keep only rate-limit timing. Every lookup makes fresh HTTP requests."""

    def __init__(self, delay=1.0):
        self.delay = delay
        self.last_request = None
        self.opener = urllib.request.build_opener()

    def fetch(self, url):
        if self.last_request is not None:
            wait = self.delay - (time.monotonic() - self.last_request)
            if wait > 0:
                time.sleep(wait)
        request = urllib.request.Request(url, headers={
            "User-Agent": "KindleAnkiCLI/1.0 (personal vocabulary lookup)",
            "Accept": "text/html",
            "Cache-Control": "no-cache, no-store",
            "Pragma": "no-cache",
        })
        self.last_request = time.monotonic()
        try:
            with self.opener.open(request, timeout=20) as response:
                final_url = response.geturl()
                parsed = urllib.parse.urlsplit(final_url)
                if parsed.scheme != "https" or parsed.netloc != "kotobank.jp":
                    raise DictionaryUnavailable("Unexpected redirect away from Kotobank.")
                raw = response.read(4_000_001)
                if len(raw) > 4_000_000:
                    raise DictionaryUnavailable("Kotobank response exceeded the page size limit.")
                charset = response.headers.get_content_charset() or "utf-8"
                return raw.decode(charset), final_url
        except urllib.error.HTTPError as error:
            error.close()
            if error.code == 404:
                return "", url
            raise DictionaryUnavailable(f"Kotobank returned HTTP {error.code}; stopped lookups.") from error
        except (urllib.error.URLError, TimeoutError, OSError, UnicodeError, LookupError) as error:
            raise DictionaryUnavailable(f"Cannot reach/read Kotobank: {error}") from error

    def lookup(self, word, reading=""):
        direct = "https://kotobank.jp/word/" + urllib.parse.quote(word, safe="")
        markup, url = self.fetch(direct)
        if markup:
            body = extract_definition(markup, word, reading)
            if body:
                return Definition(body, url)
        # Direct word URLs may lead to a search page. Follow only exact headwords.
        links = exact_entry_links(markup, word)
        if not links:
            search = "https://kotobank.jp/search?" + urllib.parse.urlencode({"q": word, "t": "all"})
            markup, url = self.fetch(search)
            if markup:
                body = extract_definition(markup, word, reading)
                if body:
                    return Definition(body, url)
            links = exact_entry_links(markup, word)
        for link in links[:5]:
            markup, url = self.fetch(link)
            if markup:
                body = extract_definition(markup, word, reading)
                if body:
                    return Definition(body, url)
        return None
