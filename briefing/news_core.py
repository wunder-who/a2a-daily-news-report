"""News fetching, filtering, de-duplication and ranking for the daily briefing.

Sections: "opensource" (HN, GitHub, OSS RSS feeds), "world" and "politics"
(public-broadcaster / newspaper RSS feeds, with Australian sources for relevance).

Pure-Python, no API keys. Every fetcher is isolated: a failing source is
recorded in the status report and never breaks the briefing.
"""
from __future__ import annotations

import asyncio
import math
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Awaitable, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import httpx

USER_AGENT = "a2a-briefing/0.2 (+daily news briefing demo)"
SECTIONS = ("opensource", "world", "politics")
TIMEOUT = httpx.Timeout(12.0, connect=6.0)

# ---------------------------------------------------------------- model


@dataclass
class NewsItem:
    title: str
    url: str
    source: str
    published: datetime  # timezone-aware UTC
    kind: str  # hn | github | rss | reddit
    points: int = 0  # HN points / GitHub stars / Reddit upvotes
    comments: int = 0
    discussion_url: str = ""
    summary: str = ""
    also_on: list[str] = field(default_factory=list)
    score: float = 0.0
    theme: str = ""
    section: str = "opensource"  # opensource | world | politics
    region: str = ""  # news sections: "au" or "intl"
    related: list[str] = field(default_factory=list)  # other outlets with a similar story

    def to_dict(self) -> dict:
        d = asdict(self)
        d["published"] = self.published.astimezone(timezone.utc).isoformat()
        d["score"] = round(self.score, 4)
        return d


@dataclass
class Query:
    topic: str = ""
    hours: int = 24
    limit: int = 15  # per section
    sections: list[str] = field(default_factory=lambda: list(SECTIONS))


# ---------------------------------------------------------------- input parsing

_STOP = {"latest", "last", "past", "news", "open", "source", "opensource", "open-source",
         "about", "on", "for", "the", "in", "of", "today", "daily", "briefing", "me", "give",
         "show", "what", "whats", "what's", "top", "and", "from", "within"}


_SECTION_ALIASES = {
    "opensource": "opensource", "open-source": "opensource", "oss": "opensource", "os": "opensource",
    "foss": "opensource", "tech": "opensource",
    "world": "world", "international": "world", "news": "world", "global": "world",
    "politics": "politics", "political": "politics", "policy": "politics",
}
_SECTION_WORDS = [
    ("opensource", r"\bopen[\s-]?source\b|\bfoss\b"),
    ("world", r"\bworld(\s+news)?\b|\binternational\b"),
    ("politics", r"\bpolitic(s|al)\b"),
]


def parse_sections(value: str) -> list[str]:
    out = []
    for part in re.split(r"[,+\s]+", value.strip().lower()):
        if part == "all":
            return list(SECTIONS)
        sec = _SECTION_ALIASES.get(part)
        if sec and sec not in out:
            out.append(sec)
    return [s for s in SECTIONS if s in out] or list(SECTIONS)


def parse_query(text: str) -> Query:
    """Parse free text like 'rust', 'AI last 48h', 'topic=kubernetes hours=72 limit=20',
    'sections=opensource,world,politics'. Default sections: all three, unless the text
    names sections ('open source news', 'world news', 'politics')."""
    q = Query()
    t = (text or "").strip()
    m = re.search(r"sections?\s*[=:]\s*([\w,+-]+)", t, re.I)
    explicit = None
    if m:
        explicit = parse_sections(m.group(1))
        t = t.replace(m.group(0), " ")
    else:
        found = [sec for sec, rx in _SECTION_WORDS if re.search(rx, t, re.I)]
        if found:
            explicit = found
            for _, rx in _SECTION_WORDS:
                t = re.sub(rx, " ", t, flags=re.I)
    if explicit:
        q.sections = explicit
    for key, val in re.findall(r"(topic|hours|limit|days)\s*[=:]\s*([^\s,]+)", t, re.I):
        key = key.lower()
        if key == "topic":
            q.topic = val
        elif key == "hours" and val.isdigit():
            q.hours = int(val)
        elif key == "days" and val.isdigit():
            q.hours = int(val) * 24
        elif key == "limit" and val.isdigit():
            q.limit = int(val)
    t = re.sub(r"(topic|hours|limit|days)\s*[=:]\s*[^\s,]+", " ", t, flags=re.I)
    m = re.search(r"(\d+)\s*(h|hrs?|hours?)\b", t, re.I)
    if m:
        q.hours = int(m.group(1))
        t = t.replace(m.group(0), " ")
    m = re.search(r"(\d+)\s*(d|days?)\b", t, re.I)
    if m:
        q.hours = int(m.group(1)) * 24
        t = t.replace(m.group(0), " ")
    if re.search(r"\bthis week\b|\bweekly\b", t, re.I):
        q.hours = 168
        t = re.sub(r"\bthis week\b|\bweekly\b", " ", t, flags=re.I)
    if not q.topic:
        words = [w for w in re.findall(r"[\w.+#-]+", t) if w.lower() not in _STOP]
        q.topic = " ".join(words)
    q.hours = max(1, min(q.hours, 24 * 30))
    q.limit = max(1, min(q.limit, 50))
    return q


# ---------------------------------------------------------------- helpers

OSS_RE = re.compile(
    r"open[\s-]?source|\boss\b|\bfoss\b|github\.com|gitlab|codeberg|\blinux\b|kernel|"
    r"\brelease[sd]?\b|\bv?\d+\.\d+(\.\d+)?\b|licen[cs]e|\bgpl\b|\bmit license\b|apache|"
    r"\bfork(ed)?\b|maintainer|\brust\b|python|postgres|kubernetes|\bcncf\b|foundation|"
    r"self[\s-]?host|\bshow hn\b",
    re.I,
)


# Stricter signal used when a topic is given (topic words like "rust"/"AI" must not
# be enough on their own to count as open source news).
OSS_CORE_RE = re.compile(
    r"open[\s-]?source|\boss\b|\bfoss\b|github\.com|gitlab|codeberg|sourcehut|licen[cs]e|"
    r"\bgpl\b|\bapache\b|\bfork(ed)?\b|maintainer|\blinux\b|self[\s-]?host",
    re.I,
)


def is_open_source_related(*texts: str, strict: bool = False) -> bool:
    rx = OSS_CORE_RE if strict else OSS_RE
    return any(t and rx.search(t) for t in texts)


def matches_topic(item: NewsItem, topic: str) -> bool:
    if not topic:
        return True
    hay = f"{item.title} {item.summary} {item.url}"
    for word in topic.split():
        pat = rf"(?<![A-Za-z0-9]){re.escape(word)}(?![A-Za-z0-9])"
        if not re.search(pat, hay, re.I):
            return False
    return True


def normalize_url(url: str) -> str:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    host = parts.netloc.lower().removeprefix("www.").removeprefix("m.")
    path = parts.path.rstrip("/") or "/"
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query)
                             if not k.lower().startswith(("utm_", "ref", "fbclid", "gclid"))))
    return urlunsplit(("", host, path, query, "")).lstrip("/")


def normalize_title(title: str) -> str:
    t = title.lower()
    t = re.sub(r"^(show|ask|launch|tell) hn:\s*", "", t)
    t = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", t)
    return " ".join(re.findall(r"[a-z0-9]+", t))


_TITLE_STOP = {"a", "an", "the", "with", "and", "or", "of", "for", "to", "in", "on", "is",
               "now", "new", "its", "it", "by", "at", "as", "from", "many", "more"}


def _sig_tokens(t: str) -> set[str]:
    return {w for w in normalize_title(t).split() if w not in _TITLE_STOP}


def titles_similar(a: str, b: str, threshold: float = 0.85) -> bool:
    """Same story? Near-identical text, or one title's key words (>=3, incl. a number
    or a long proper word) almost all contained in the other's."""
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return False
    if na == nb or SequenceMatcher(None, na, nb).ratio() >= threshold:
        return True
    ta, tb = _sig_tokens(a), _sig_tokens(b)
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if len(small) < 3:
        return False
    overlap = len(small & big) / len(small)
    has_anchor = any(w.isdigit() for w in small & big)
    return overlap >= 0.75 and has_anchor


def _ts(dt_struct) -> datetime | None:
    if not dt_struct:
        return None
    # feedparser normalises *_parsed fields to UTC
    return datetime(*dt_struct[:6], tzinfo=timezone.utc)


def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s[:280]


# ---------------------------------------------------------------- fetchers

HN_QUERIES = ["open source", "open-source", "github", "release", "linux", "license"]


async def fetch_hn(client: httpx.AsyncClient, q: Query) -> list[NewsItem]:
    since = int(time.time() - q.hours * 3600)
    queries = [q.topic] if q.topic else HN_QUERIES

    async def one(term: str) -> list[dict]:
        r = await client.get(
            "https://hn.algolia.com/api/v1/search",
            params={"query": term, "tags": "story", "hitsPerPage": 50,
                    "numericFilters": f"created_at_i>{since},points>=3"},
        )
        r.raise_for_status()
        return r.json().get("hits", [])

    results = await asyncio.gather(*(one(t) for t in queries), return_exceptions=True)
    hits: dict[str, dict] = {}
    errors = [r for r in results if isinstance(r, Exception)]
    if errors and len(errors) == len(results):
        raise errors[0]
    for res in results:
        if isinstance(res, Exception):
            continue
        for h in res:
            hits[h["objectID"]] = h
    items = []
    for h in hits.values():
        title = h.get("title") or ""
        url = h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}"
        if not title:
            continue
        if not is_open_source_related(title, url, strict=bool(q.topic)):
            continue
        items.append(NewsItem(
            title=title, url=url, source="Hacker News",
            published=datetime.fromtimestamp(h["created_at_i"], tz=timezone.utc),
            kind="hn", points=int(h.get("points") or 0),
            comments=int(h.get("num_comments") or 0),
            discussion_url=f"https://news.ycombinator.com/item?id={h['objectID']}",
        ))
    return items


GITHUB_MIN_WINDOW_H = 168  # "trending-like": repos created in the last 7 days by stars


async def fetch_github(client: httpx.AsyncClient, q: Query) -> list[NewsItem]:
    hours = max(q.hours, GITHUB_MIN_WINDOW_H)
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d")
    query = f"created:>{since}" + (f" {q.topic}" if q.topic else "")
    r = await client.get(
        "https://api.github.com/search/repositories",
        params={"q": query, "sort": "stars", "order": "desc", "per_page": 15},
        headers={"Accept": "application/vnd.github+json"},
    )
    if r.status_code in (403, 429):
        reset = r.headers.get("x-ratelimit-reset")
        raise RuntimeError(f"GitHub rate limited (HTTP {r.status_code}, reset={reset})")
    r.raise_for_status()
    items = []
    for repo in r.json().get("items", []):
        desc = repo.get("description") or ""
        lang = repo.get("language") or ""
        title = repo["full_name"] + (f": {desc}" if desc else "")
        items.append(NewsItem(
            title=title[:200], url=repo["html_url"], source="GitHub (new & rising)",
            published=datetime.fromisoformat(repo["created_at"].replace("Z", "+00:00")),
            kind="github", points=int(repo.get("stargazers_count") or 0),
            summary=(f"[{lang}] " if lang else "") + desc[:240],
        ))
    return items


@dataclass
class Feed:
    name: str
    url: str
    filter_oss: bool = False  # general-tech feeds get an open-source keyword filter
    section: str = "opensource"
    region: str = ""  # "au" for Australian outlets / editions, "intl" otherwise


FEEDS = [
    Feed("LWN.net", "https://lwn.net/headlines/rss"),
    Feed("GitHub Blog", "https://github.blog/feed/"),
    Feed("Linux Foundation", "https://www.linuxfoundation.org/blog/rss.xml"),
    Feed("Opensource.com", "https://opensource.com/feed"),
    Feed("The New Stack", "https://thenewstack.io/feed/", filter_oss=True),
    Feed("Phoronix", "https://www.phoronix.com/rss.php"),
]

# World & politics: a mix of public broadcasters and newspapers from several countries,
# with Australian outlets for local relevance. All verified reachable without keys.
NEWS_FEEDS = [
    # world
    Feed("ABC News (Just In)", "https://www.abc.net.au/news/feed/51120/rss.xml", section="world"),
    Feed("ABC News (World)", "https://www.abc.net.au/news/feed/104217382/rss.xml", section="world", region="intl"),
    Feed("Guardian Australia", "https://www.theguardian.com/australia-news/rss", section="world", region="au"),
    Feed("Guardian World", "https://www.theguardian.com/world/rss", section="world", region="intl"),
    Feed("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml", section="world", region="intl"),
    Feed("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", section="world", region="intl"),
    Feed("DW", "https://rss.dw.com/xml/rss-en-world", section="world", region="intl"),
    Feed("NPR World", "https://feeds.npr.org/1004/rss.xml", section="world", region="intl"),
    Feed("SBS News (World)", "https://www.sbs.com.au/news/topic/world/feed", section="world", region="intl"),
    # politics
    Feed("Guardian Australian Politics", "https://www.theguardian.com/australia-news/australian-politics/rss",
         section="politics", region="au"),
    Feed("BBC Politics", "https://feeds.bbci.co.uk/news/politics/rss.xml", section="politics", region="intl"),
    Feed("Guardian UK Politics", "https://www.theguardian.com/politics/rss", section="politics", region="intl"),
    Feed("Guardian US Politics", "https://www.theguardian.com/us-news/us-politics/rss", section="politics", region="intl"),
    Feed("NPR Politics", "https://feeds.npr.org/1014/rss.xml", section="politics", region="intl"),
]

OPINION_RE = re.compile(r"/commentisfree/|/opinion/|/opinions/|/podcasts?/", re.I)
# world/politics: skip lifestyle, culture and sport sections that general feeds mix in
NON_NEWS_RE = re.compile(r"/(lifeandstyle|culture|film|music|books|food|fashion|travel|sports?|"
                         r"football|tv-and-radio|stage|games|artanddesign|wellness|recipes)/", re.I)
BOILERPLATE_RE = re.compile(r"\s*(Get our breaking news email|Sign up for |Continue reading)\b.*$", re.I)
OPINION_TITLE_RE = re.compile(r"[-–]\s*podcast\s*$|^\s*opinion\s*[:|]", re.I)


def outlet(source: str) -> str:
    """Organisation behind a source name: 'Guardian World' -> 'Guardian'."""
    return source.split(" ")[0]


AU_RE = re.compile(r"\baustrali(a|an|ans)\b|\balbanese\b|\bcanberra\b|\bperth\b|"
                   r"\bwestern australia\b|\bnew south wales\b|\bvictoria(n)?\b|\bqueensland\b|"
                   r"\bsydney\b|\bmelbourne\b|\badelaide\b|\bbrisbane\b|\btasmania\b", re.I)


async def fetch_feed(client: httpx.AsyncClient, q: Query, feed: Feed) -> list[NewsItem]:
    r = await client.get(feed.url)
    r.raise_for_status()
    parsed = feedparser.parse(r.content)
    if parsed.bozo and not parsed.entries:
        raise RuntimeError(f"unparseable feed: {parsed.bozo_exception}")
    items = []
    for e in parsed.entries:
        published = _ts(e.get("published_parsed") or e.get("updated_parsed"))
        title, link = (e.get("title") or "").strip(), (e.get("link") or "").strip()
        if not (published and title and link):
            continue
        summary = _strip_html(e.get("summary", ""))
        tags = " ".join(t.get("term", "") for t in e.get("tags", []) or [])
        if feed.filter_oss and not is_open_source_related(title, summary, tags):
            continue
        it = NewsItem(title=title, url=link, source=feed.name, published=published,
                      kind="rss", summary=summary, section=feed.section)
        if feed.section != "opensource":
            if OPINION_RE.search(link) or OPINION_TITLE_RE.search(title) or NON_NEWS_RE.search(link):
                continue  # news briefing: skip opinion, podcasts, lifestyle/sport
            it.summary = BOILERPLATE_RE.sub("", summary).strip()
            it.region = "au" if (feed.region == "au" or AU_RE.search(f"{title} {summary}")) else "intl"
        items.append(it)
    return items


async def fetch_reddit(client: httpx.AsyncClient, q: Query) -> list[NewsItem]:
    try:
        r = await client.get("https://www.reddit.com/r/opensource/top.json",
                             params={"t": "week" if q.hours > 24 else "day", "limit": 25})
    except httpx.HTTPError as e:
        raise RuntimeError(f"Reddit unreachable ({type(e).__name__}); "
                           "unauthenticated JSON access is usually blocked") from e
    if r.status_code != 200:
        raise RuntimeError(f"Reddit returned HTTP {r.status_code} (unauthenticated access blocked)")
    items = []
    for c in r.json().get("data", {}).get("children", []):
        d = c.get("data", {})
        permalink = "https://www.reddit.com" + d.get("permalink", "")
        items.append(NewsItem(
            title=d.get("title", ""), url=d.get("url") or permalink, source="r/opensource",
            published=datetime.fromtimestamp(d.get("created_utc", 0), tz=timezone.utc),
            kind="reddit", points=int(d.get("score") or 0),
            comments=int(d.get("num_comments") or 0), discussion_url=permalink,
        ))
    return items


SourceFn = Callable[[httpx.AsyncClient, Query], Awaitable[list[NewsItem]]]


def all_sources(q: Query | None = None) -> list[tuple[str, SourceFn]]:
    """(name, fetcher) for every source in the requested sections."""
    sections = q.sections if q else list(SECTIONS)
    srcs: list[tuple[str, SourceFn]] = []
    if "opensource" in sections:
        srcs += [("Hacker News", fetch_hn), ("GitHub (new & rising)", fetch_github)]
        srcs += [(f.name, lambda c, q, f=f: fetch_feed(c, q, f)) for f in FEEDS]
        srcs.append(("r/opensource", fetch_reddit))
    for f in NEWS_FEEDS:
        if f.section in sections:
            srcs.append((f.name, lambda c, q, f=f: fetch_feed(c, q, f)))
    return srcs


SOURCE_SECTION = {name: "opensource" for name in
                  ["Hacker News", "GitHub (new & rising)", "r/opensource", *(f.name for f in FEEDS)]}
SOURCE_SECTION.update({f.name: f.section for f in NEWS_FEEDS})


# ---------------------------------------------------------------- ranking

THEMES = [
    ("Security", r"securit|vulnerab|\bcve\b|exploit|malware|supply[\s-]chain|backdoor|patch"),
    ("AI & ML", r"\bai\b|\bllms?\b|machine learning|model|agent|gpt|inference|neural|ml\b"),
    ("Linux & Kernel", r"\blinux\b|kernel|distro|debian|ubuntu|fedora|opensuse|\barch\b|gnome|kde|wayland|mesa"),
    ("Releases", r"\breleased?\b|\breleases\b|\bv\d+(\.\d+)+\b|launch|now available|announc"),
    ("Licensing & Community", r"licen[cs]|\bgpl\b|foundation|governance|community|maintainer|funding|fork"),
    ("Languages & Dev Tools", r"\brust\b|python|golang|\bgo\b|javascript|typescript|compiler|zig|"
                              r"\bjava\b|postgres|database|kubernetes|docker|devtools?|\bcli\b|framework"),
]


def classify(item: NewsItem) -> str:
    if item.kind == "github":
        return "New & Rising on GitHub"
    hay = f"{item.title} {item.summary}"
    for name, pat in THEMES:
        if re.search(pat, hay, re.I):
            return name
    return "Other Open Source News"


def score_item(item: NewsItem, now: datetime, hours: int) -> float:
    """Open source: popularity (log-scaled per source type) + recency + cross-source bonus.
    World/politics (RSS only, no popularity signal): recency + multi-source coverage."""
    age_h = max(0.0, (now - item.published).total_seconds() / 3600)
    half_life = max(6.0, hours / 2)
    recency = 0.5 ** (age_h / half_life)
    if item.section != "opensource":
        coverage = len({outlet(s) for s in (*item.also_on, *item.related)} - {outlet(item.source)})
        return 0.7 * recency + 0.12 * min(coverage, 3) + (0.05 if item.region == "au" else 0.0)
    if item.kind == "hn":
        pop = min(1.0, math.log1p(item.points) / math.log1p(500))
    elif item.kind == "github":
        pop = min(1.0, math.log1p(item.points) / math.log1p(3000))
    elif item.kind == "reddit":
        pop = min(1.0, math.log1p(item.points) / math.log1p(300))
    else:  # curated editorial feeds: steady baseline
        pop = 0.45
    bonus = 0.1 * len(item.also_on)
    return 0.65 * pop + 0.35 * recency + bonus


def dedupe(items: list[NewsItem]) -> list[NewsItem]:
    """Merge items sharing a normalized URL or near-identical title.
    Keeps the higher-scoring item and records the other sources in `also_on`."""
    kept: list[NewsItem] = []
    by_url: dict[str, NewsItem] = {}
    for it in sorted(items, key=lambda i: i.score, reverse=True):
        key = normalize_url(it.url)
        dup = by_url.get(key)
        if dup is None:
            dup = next((k for k in kept if titles_similar(k.title, it.title)), None)
        if dup is not None:
            if it.source != dup.source and it.source not in dup.also_on:
                dup.also_on.append(it.source)
            if not dup.discussion_url and it.discussion_url:
                dup.discussion_url = it.discussion_url
            continue
        kept.append(it)
        by_url[key] = it
    return kept


def _story_tokens(title: str) -> set[str]:
    return {w for w in _sig_tokens(title) if len(w) >= 4 or w.isdigit()}


def mark_related(items: list[NewsItem]) -> None:
    """Record other outlets carrying a similar story (shared key words), for ranking.
    Looser than dedupe: items are kept separate, each outlet's own headline is shown."""
    toks = [_story_tokens(i.title) for i in items]
    for a, ta in zip(items, toks):
        for b, tb in zip(items, toks):
            if a is b or outlet(a.source) == outlet(b.source) or not ta or not tb:
                continue
            shared = ta & tb
            if len(shared) >= 3 and len(shared) / len(ta | tb) >= 0.3 and b.source not in a.related:
                a.related.append(b.source)


def rank(items: list[NewsItem], q: Query, now: datetime | None = None,
         max_per_source: int | None = None) -> list[NewsItem]:
    """Rank each requested section separately; returns sections in SECTIONS order."""
    now = now or datetime.now(timezone.utc)
    out: list[NewsItem] = []
    # a story in the politics feeds is shown under Politics only
    politics_urls = ({normalize_url(i.url) for i in items if i.section == "politics"}
                     if "politics" in q.sections else set())
    for sec in SECTIONS:
        if sec in q.sections:
            pool = [i for i in items if i.section == sec and
                    not (sec == "world" and normalize_url(i.url) in politics_urls)]
            out += _rank_section(pool, q, sec, now, max_per_source)
    return out


def _rank_section(items: list[NewsItem], q: Query, section: str, now: datetime,
                  max_per_source: int | None) -> list[NewsItem]:
    if max_per_source is None:  # keep each section diverse
        max_per_source = (max(3, math.ceil(q.limit * 0.3)) if section == "opensource"
                          else max(2, math.ceil(q.limit * 0.25)))
    cutoff = now - timedelta(hours=q.hours)
    pool = [i for i in items
            if (i.published >= cutoff or (i.kind == "github" and
                i.published >= now - timedelta(hours=max(q.hours, GITHUB_MIN_WINDOW_H))))
            and matches_topic(i, q.topic)]
    for i in pool:
        i.score = score_item(i, now, q.hours)
    merged = dedupe(pool)
    if section != "opensource":
        mark_related(merged)
    for i in merged:  # re-score with cross-source bonus
        i.score = score_item(i, now, q.hours)
        i.theme = classify(i) if section == "opensource" else (
            "Australia" if i.region == "au" else "International")
    merged.sort(key=lambda i: (-i.score, i.title))
    out, per_src = [], {}

    def take(candidates, quota):
        for i in candidates:
            if quota <= 0 or len(out) >= q.limit:
                return
            if i in out:
                continue
            key = i.source if section == "opensource" else (outlet(i.source), i.region)
            if per_src.get(key, 0) >= max_per_source:
                continue
            per_src[key] = per_src.get(key, 0) + 1
            out.append(i)
            quota -= 1

    if section != "opensource":  # local relevance: reserve ~3/8 of news slots for Australia
        take([i for i in merged if i.region == "au"], math.ceil(q.limit * 0.375))
    take(merged, q.limit)  # keep the briefing diverse: cap per source (news: per outlet+region)
    out.sort(key=lambda i: (-i.score, i.title))
    return out


# ---------------------------------------------------------------- orchestration

ProgressCb = Callable[[str, dict], Awaitable[None]]


async def gather_news(q: Query, progress: ProgressCb | None = None,
                      client: httpx.AsyncClient | None = None,
                      sources=None) -> tuple[list[NewsItem], dict]:
    sources = sources or all_sources(q)
    own = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True,
                                         headers={"User-Agent": USER_AGENT})
    status: dict[str, dict] = {}
    collected: list[NewsItem] = []

    async def run(name, fn):
        t0 = time.monotonic()
        try:
            res = await asyncio.wait_for(fn(client, q), timeout=25)
            st = {"ok": True, "fetched": len(res), "error": ""}
        except Exception as e:  # noqa: BLE001 - one bad source must not kill the briefing
            res = []
            st = {"ok": False, "fetched": 0, "error": f"{type(e).__name__}: {e}"[:200]}
        st["seconds"] = round(time.monotonic() - t0, 2)
        st["section"] = SOURCE_SECTION.get(name, res[0].section if res else "")
        status[name] = st
        collected.extend(res)
        if progress:
            await progress(name, st)

    try:
        await asyncio.gather(*(run(n, f) for n, f in sources))
    finally:
        if own:
            await client.aclose()
    ranked = rank(collected, q)
    for name in status:
        status[name]["in_briefing"] = sum(1 for i in ranked if i.source == name)
    return ranked, status
