from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

import news_core as nc
from news_core import NewsItem, Query

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


def item(title, url, source="LWN.net", kind="rss", hours_ago=1, points=0, **kw):
    return NewsItem(title=title, url=url, source=source, kind=kind,
                    published=NOW - timedelta(hours=hours_ago), points=points, **kw)


# ---------------- parsing / normalisation

@pytest.mark.parametrize("text,topic,hours,limit", [
    ("", "", 24, 15),
    ("rust", "rust", 24, 15),
    ("AI last 48h", "AI", 48, 15),
    ("latest open source news about kubernetes 3 days", "kubernetes", 72, 15),
    ("topic=linux hours=72 limit=20", "linux", 72, 20),
    ("hours=99999", "", 720, 15),
])
def test_parse_query(text, topic, hours, limit):
    q = nc.parse_query(text)
    assert (q.topic, q.hours, q.limit) == (topic, hours, limit)


def test_normalize_url_strips_tracking_and_www():
    a = nc.normalize_url("https://www.example.com/post/?utm_source=x&id=2")
    b = nc.normalize_url("http://example.com/post?id=2")
    assert a == b


def test_titles_similar():
    assert nc.titles_similar("Fedora Linux 45 released",
                             "Fedora 45 Released With Many Leading-Edge Linux Innovations")
    assert nc.titles_similar("Show HN: Foo – a tool", "Foo – a tool")
    assert not nc.titles_similar("Rust 1.90 released", "Go 1.30 released with generics speedups")
    assert not nc.titles_similar("Security updates for Monday", "Kernel prepatch 6.20-rc3")


def test_topic_match_word_boundary():
    assert nc.matches_topic(item("New AI agent framework", "https://x/1"), "AI")
    assert not nc.matches_topic(item("Maintainer burnout", "https://x/2"), "AI")


# ---------------- dedupe / ranking

def test_dedupe_by_url_and_title_records_other_sources():
    items = [
        item("Fedora Linux 45 released", "https://lwn.net/Articles/1/", score=0.9),
        item("Fedora 45 Released With Linux Innovations", "https://phoronix.com/f45",
             source="Phoronix", score=0.5),
        item("Something", "https://www.lwn.net/Articles/1?utm_source=hn", source="Hacker News",
             kind="hn", score=0.4, discussion_url="https://news.ycombinator.com/item?id=1"),
        item("Unrelated", "https://other/x", source="Phoronix", score=0.3),
    ]
    out = nc.dedupe(items)
    assert [i.title for i in out] == ["Fedora Linux 45 released", "Unrelated"]
    assert set(out[0].also_on) == {"Phoronix", "Hacker News"}
    assert out[0].discussion_url == "https://news.ycombinator.com/item?id=1"


def test_rank_window_topic_order_and_cap():
    items = [
        item("Old rust story", "https://a/old", hours_ago=50),
        item("Rust 2.0 released", "https://a/1", kind="hn", source="Hacker News", points=400),
        item("Rust tooling news", "https://a/2", hours_ago=10),
        item("Python news", "https://a/3"),
    ]
    out = nc.rank(items, Query(topic="rust", hours=24, limit=10), now=NOW)
    assert [i.url for i in out] == ["https://a/1", "https://a/2"]
    assert out[0].score > out[1].score
    words = ["kernel", "gnome", "postgres", "ruby", "zig", "debian", "mesa", "kde", "qemu", "nginx"]
    many = [item(f"{w} update", f"https://a/{w}") for w in words]
    capped = nc.rank(many, Query(limit=10), now=NOW)
    assert len(capped) == 3  # max ~30% of the briefing from one source


def test_github_items_use_7_day_window():
    repo = item("org/repo: thing", "https://github.com/org/repo", source="GitHub (new & rising)",
                kind="github", hours_ago=100, points=500)
    assert nc.rank([repo], Query(hours=24), now=NOW)[0].theme == "New & Rising on GitHub"


def test_score_prefers_popular_and_recent():
    hot = item("a", "u1", kind="hn", points=500, hours_ago=1)
    cold = item("b", "u2", kind="hn", points=5, hours_ago=20)
    assert nc.score_item(hot, NOW, 24) > nc.score_item(cold, NOW, 24)


# ---------------- fetchers with mocked HTTP

@respx.mock
async def test_fetch_hn_filters_and_dedupes_hits():
    ts = int(datetime.now(timezone.utc).timestamp()) - 3600
    hits = {"hits": [
        {"objectID": "1", "title": "Project X goes open source", "url": "https://x.dev",
         "created_at_i": ts, "points": 120, "num_comments": 30},
        {"objectID": "2", "title": "My cooking blog", "url": "https://food.example",
         "created_at_i": ts, "points": 50, "num_comments": 1},
        {"objectID": "3", "title": "Ask HN: anything", "url": None, "created_at_i": ts,
         "points": 10, "num_comments": 4},
    ]}
    respx.get("https://hn.algolia.com/api/v1/search").mock(return_value=httpx.Response(200, json=hits))
    async with httpx.AsyncClient() as c:
        out = await nc.fetch_hn(c, Query())
    assert [i.title for i in out] == ["Project X goes open source"]
    assert out[0].points == 120 and out[0].discussion_url.endswith("id=1")


@respx.mock
async def test_fetch_github_parses_and_reports_rate_limit():
    route = respx.get("https://api.github.com/search/repositories")
    route.mock(return_value=httpx.Response(200, json={"items": [{
        "full_name": "a/b", "description": "fast thing", "html_url": "https://github.com/a/b",
        "created_at": "2026-11-01T00:00:00Z", "stargazers_count": 999, "language": "Rust"}]}))
    async with httpx.AsyncClient() as c:
        out = await nc.fetch_github(c, Query(topic="rust"))
        assert out[0].url == "https://github.com/a/b" and out[0].points == 999
        assert "rust" in route.calls.last.request.url.params["q"]
        route.mock(return_value=httpx.Response(403, headers={"x-ratelimit-reset": "123"}))
        with pytest.raises(RuntimeError, match="rate limited"):
            await nc.fetch_github(c, Query())


RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>
<item><title>Kubernetes 1.40 released</title><link>https://ex.com/k8s</link>
<pubDate>Tue, 03 Nov 2026 10:00:00 GMT</pubDate><description>open source news</description></item>
<item><title>Cloud cost tips</title><link>https://ex.com/cost</link>
<pubDate>Tue, 03 Nov 2026 09:00:00 GMT</pubDate><description>save money</description></item>
<item><title>No date</title><link>https://ex.com/nodate</link></item>
</channel></rss>"""


@respx.mock
async def test_fetch_feed_parses_and_filters():
    respx.get("https://feed.test/rss").mock(return_value=httpx.Response(200, content=RSS))
    async with httpx.AsyncClient() as c:
        all_items = await nc.fetch_feed(c, Query(), nc.Feed("F", "https://feed.test/rss"))
        oss = await nc.fetch_feed(c, Query(), nc.Feed("F", "https://feed.test/rss", filter_oss=True))
    assert [i.url for i in all_items] == ["https://ex.com/k8s", "https://ex.com/cost"]
    assert all_items[0].published == datetime(2026, 11, 3, 10, tzinfo=timezone.utc)
    assert [i.url for i in oss] == ["https://ex.com/k8s"]


@respx.mock
async def test_reddit_403_raises_cleanly():
    respx.get("https://www.reddit.com/r/opensource/top.json").mock(return_value=httpx.Response(403))
    async with httpx.AsyncClient() as c:
        with pytest.raises(RuntimeError, match="403"):
            await nc.fetch_reddit(c, Query())


async def test_gather_news_isolates_failing_source():
    now = datetime.now(timezone.utc)

    async def good(c, q):
        return [NewsItem("Linux 7.0 released", "https://k/7", "Good", now, "rss")]

    async def bad(c, q):
        raise httpx.ConnectTimeout("boom")

    events = []

    async def progress(name, st):
        events.append((name, st["ok"]))

    async with httpx.AsyncClient() as c:
        items, status = await nc.gather_news(Query(), progress, client=c,
                                             sources=[("Good", good), ("Bad", bad)])
    assert [i.url for i in items] == ["https://k/7"]
    assert status["Good"]["ok"] and status["Good"]["in_briefing"] == 1
    assert not status["Bad"]["ok"] and "ConnectTimeout" in status["Bad"]["error"]
    assert sorted(events) == [("Bad", False), ("Good", True)]


@respx.mock
async def test_fetch_hn_topic_mode_requires_open_source_signal():
    ts = int(datetime.now(timezone.utc).timestamp()) - 3600
    hits = {"hits": [
        {"objectID": "1", "title": "AI stocks rally", "url": "https://news.example/ai",
         "created_at_i": ts, "points": 300, "num_comments": 9},
        {"objectID": "2", "title": "Open-source AI agent toolkit", "url": "https://x.dev/a",
         "created_at_i": ts, "points": 40, "num_comments": 2},
        {"objectID": "3", "title": "My AI side project", "url": "https://github.com/me/p",
         "created_at_i": ts, "points": 20, "num_comments": 2},
    ]}
    route = respx.get("https://hn.algolia.com/api/v1/search").mock(
        return_value=httpx.Response(200, json=hits))
    async with httpx.AsyncClient() as c:
        out = await nc.fetch_hn(c, Query(topic="AI"))
    assert sorted(i.url for i in out) == ["https://github.com/me/p", "https://x.dev/a"]
    assert route.calls.last.request.url.params["query"] == "AI"
