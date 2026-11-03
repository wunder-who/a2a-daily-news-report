"""World / politics sections: parsing, feeds, ranking, rendering."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

import news_core as nc
from news_core import NewsItem, Query
from render import render_markdown

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


def news(title, url, source, section="world", region="intl", hours_ago=1, summary=""):
    return NewsItem(title=title, url=url, source=source, kind="rss", section=section,
                    region=region, published=NOW - timedelta(hours=hours_ago), summary=summary)


@pytest.mark.parametrize("text,sections,topic", [
    ("", ["opensource", "world", "politics"], ""),
    ("sections=opensource", ["opensource"], ""),
    ("sections=politics,world rust", ["world", "politics"], "rust"),
    ("sections=oss+international", ["opensource", "world"], ""),
    ("sections=all", ["opensource", "world", "politics"], ""),
    ("sections=bogus", ["opensource", "world", "politics"], ""),
    ("world news last 12h", ["world"], ""),
    ("politics climate", ["politics"], "climate"),
    ("latest open source news about kubernetes", ["opensource"], "kubernetes"),
])
def test_parse_sections(text, sections, topic):
    q = nc.parse_query(text)
    assert q.sections == sections and q.topic == topic


def test_all_sources_respects_sections():
    names = [n for n, _ in nc.all_sources(Query(sections=["politics"]))]
    assert "BBC Politics" in names and "Guardian Australian Politics" in names
    assert "Hacker News" not in names and "BBC World" not in names
    os_names = [n for n, _ in nc.all_sources(Query(sections=["opensource"]))]
    assert "Hacker News" in os_names and "BBC World" not in os_names


NEWS_RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>
<item><title>Election called in Western Australia</title><link>https://n.test/wa</link>
<pubDate>Tue, 03 Nov 2026 10:00:00 GMT</pubDate><description>Voters will go to the polls.</description></item>
<item><title>Summit opens in Geneva</title><link>https://n.test/geneva</link>
<pubDate>Tue, 03 Nov 2026 09:00:00 GMT</pubDate><description>Leaders meet. Get our breaking news email, free app</description></item>
<item><title>Why the summit will fail</title><link>https://n.test/commentisfree/x</link>
<pubDate>Tue, 03 Nov 2026 09:00:00 GMT</pubDate></item>
<item><title>My favourite recipe</title><link>https://n.test/lifeandstyle/2026/recipe</link>
<pubDate>Tue, 03 Nov 2026 09:00:00 GMT</pubDate></item>
<item><title>Match report</title><link>https://n.test/sport/2026/match</link>
<pubDate>Tue, 03 Nov 2026 09:00:00 GMT</pubDate></item>
<item><title>Weekly wrap - podcast</title><link>https://n.test/audio/1</link>
<pubDate>Tue, 03 Nov 2026 09:00:00 GMT</pubDate></item>
</channel></rss>"""


@respx.mock
async def test_news_feed_region_and_opinion_filter():
    respx.get("https://n.test/rss").mock(return_value=httpx.Response(200, content=NEWS_RSS))
    feed = nc.Feed("Test World", "https://n.test/rss", section="world", region="intl")
    async with httpx.AsyncClient() as c:
        out = await nc.fetch_feed(c, Query(), feed)
    assert [(i.url, i.region, i.section) for i in out] == [
        ("https://n.test/wa", "au", "world"), ("https://n.test/geneva", "intl", "world")]
    assert out[0].summary == "Voters will go to the polls."  # feed snippet kept verbatim
    assert out[1].summary == "Leaders meet."  # newsletter boilerplate trimmed


@respx.mock
async def test_failing_news_feed_is_isolated():
    respx.get("https://ok.test/rss").mock(return_value=httpx.Response(200, content=NEWS_RSS))
    respx.get("https://down.test/rss").mock(side_effect=httpx.ConnectError("down"))
    feeds = [nc.Feed("Up", "https://ok.test/rss", section="world"),
             nc.Feed("Down", "https://down.test/rss", section="world")]
    srcs = [(f.name, lambda c, q, f=f: nc.fetch_feed(c, q, f)) for f in feeds]
    async with httpx.AsyncClient() as c:
        items, status = await nc.gather_news(Query(sections=["world"], hours=24 * 30),
                                             client=c, sources=srcs)
    assert status["Up"]["ok"] and not status["Down"]["ok"]
    assert "ConnectError" in status["Down"]["error"]
    assert {i.url for i in items} >= {"https://n.test/wa", "https://n.test/geneva"}


def test_coverage_across_outlets_boosts_rank_but_same_outlet_does_not():
    items = [
        news("Earthquake strikes Chile coastal city", "https://a/1", "BBC World", hours_ago=3),
        news("Chile earthquake: coastal city damaged", "https://b/1", "DW", hours_ago=3),
        news("Lone story about a festival opening", "https://c/1", "Al Jazeera", hours_ago=1),
        news("Chile earthquake leaves coastal city in ruins", "https://g/1", "Guardian World",
             hours_ago=1),
        news("Australians caught in Chile earthquake coastal city", "https://g/2",
             "Guardian Australia", region="au", hours_ago=1),
    ]
    out = nc.rank(items, Query(sections=["world"], limit=10), now=NOW)
    bbc = next(i for i in out if i.source == "BBC World")
    assert set(bbc.related) >= {"DW", "Guardian World"}
    g = next(i for i in out if i.source == "Guardian World")
    assert "Guardian Australia" not in g.related  # same outlet is not independent coverage
    festival = next(i for i in out if i.source == "Al Jazeera")
    assert bbc.score > festival.score  # older but covered by several outlets


def test_news_outlet_cap_and_australia_share():
    heads = iter(["Ceasefire talks resume", "Floods displace thousands", "Central bank holds rates",
                  "Volcano erupts near capital", "Strike halts rail network", "Vote count delayed",
                  "Drought hits harvest", "Ferry capsizes offshore", "Summit agrees climate fund",
                  "Wildfire forces evacuations", "Court overturns ruling"])
    intl = [news(next(heads), f"https://i/{w}", f"Guardian {w}", hours_ago=0.5)
            for w in ["World", "Europe", "Asia", "Africa"]]
    intl += [news(next(heads), f"https://bbc/{w}", "BBC World", hours_ago=0.5)
             for w in ["alpha", "beta", "gamma", "delta"]]
    intl += [news(next(heads), f"https://aj/{w}", "Al Jazeera", hours_ago=0.5)
             for w in ["one", "two", "three"]]
    au = [news(t, f"https://au/{n}", src, region="au", hours_ago=20)
          for n, (t, src) in enumerate([("Perth heatwave warning", "ABC News (Just In)"),
                                        ("Darwin port sale reviewed", "ABC News (Just In)"),
                                        ("Hobart stadium approved", "ABC News (Just In)"),
                                        ("Cairns reef survey published", "Guardian Australia")])]
    out = nc.rank(intl + au, Query(sections=["world"], limit=8), now=NOW)
    assert len(out) == 8
    assert sum(1 for i in out if nc.outlet(i.source) == "Guardian" and i.region == "intl") == 2
    assert sum(1 for i in out if i.source == "ABC News (Just In)") == 2  # cap per outlet+region
    assert sum(1 for i in out if i.source == "BBC World") == 2
    assert sum(1 for i in out if i.region == "au") >= 3
    assert [i.score for i in out] == sorted((i.score for i in out), reverse=True)


def test_politics_takes_precedence_over_world_for_same_url():
    items = [news("Election day", "https://x/elect", "Guardian World"),
             news("Election day", "https://x/elect", "Guardian US Politics", section="politics")]
    out = nc.rank(items, Query(sections=["world", "politics"]), now=NOW)
    assert [(i.section, i.source) for i in out] == [("politics", "Guardian US Politics")]


def _payload(sections, items):
    return {"query": {"topic": "", "hours": 24, "limit": 8, "sections": sections},
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "items": items,
            "sources": {"BBC World": {"ok": True, "fetched": 3, "error": "", "section": "world",
                                      "in_briefing": 1},
                        "NPR Politics": {"ok": False, "fetched": 0, "error": "HTTPStatusError: 503",
                                         "section": "politics"}}}


def test_render_sections_source_and_verbatim_headline():
    now = datetime.now(timezone.utc).isoformat()
    it = {"title": "Summit opens in Geneva", "url": "https://n.test/geneva", "source": "BBC World",
          "published": now, "kind": "rss", "section": "world", "theme": "International",
          "summary": "Leaders meet.", "also_on": [], "related": ["DW"]}
    md = render_markdown(_payload(["world", "politics"], [it]))
    assert "# Daily Briefing" in md and "## World" in md and "### International" in md
    assert "## Politics" in md and "_No items in this window._" in md
    assert "[Summit opens in Geneva](https://n.test/geneva) — **BBC World**" in md
    assert "> Leaders meet." in md and "similar coverage: DW" in md
    assert "NPR Politics ❌" in md


def test_render_opensource_only_keeps_previous_layout():
    md = render_markdown(_payload(["opensource"], []))
    assert md.startswith("# Open Source Daily Briefing")
    assert "## World" not in md
