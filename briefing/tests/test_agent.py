"""End-to-end over A2A (in-process ASGI transport) with the news fetchers stubbed."""
from datetime import datetime, timezone

import httpx

import news_agent
import news_core
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import get_data_parts, new_text_message
from a2a.types import Role, SendMessageRequest
from render import render_markdown


async def fake_gather(q, progress=None, **kw):
    now = datetime.now(timezone.utc)
    it = news_core.NewsItem("Rust 2.0 released", "https://blog.rust-lang.org/x", "Hacker News",
                            now, "hn", points=300, theme="Releases")
    if progress:
        await progress("Hacker News", {"ok": True, "fetched": 1, "error": "", "seconds": 0.1})
    return [it], {"Hacker News": {"ok": True, "fetched": 1, "error": "", "seconds": 0.1,
                                  "in_briefing": 1}}


async def _run(monkeypatch, streaming, text="rust last 48h"):
    monkeypatch.setattr(news_agent, "gather_news", fake_gather)
    app = news_agent.build_app("127.0.0.1", 9998)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:9998") as http:
        card = await A2ACardResolver(httpx_client=http, base_url="http://127.0.0.1:9998").get_agent_card()
        assert {"latest_open_source_news", "world_news", "politics_news", "daily_briefing"} <= {
            s.id for s in card.skills}
        client = await create_client(agent=card, client_config=ClientConfig(
            streaming=streaming, httpx_client=http))
        req = SendMessageRequest(message=new_text_message(text, role=Role.ROLE_USER))
        events = [e async for e in client.send_message(req)]
    arts = []
    for e in events:
        if e.HasField("task"):
            arts += list(e.task.artifacts)
        if e.HasField("artifact_update"):
            arts.append(e.artifact_update.artifact)
    payload = next(d for a in arts for d in get_data_parts(a.parts))
    return events, payload


async def test_agent_non_streaming(monkeypatch):
    _, payload = await _run(monkeypatch, streaming=False)
    assert payload["query"]["topic"] == "rust" and payload["query"]["hours"] == 48
    assert payload["items"][0]["url"] == "https://blog.rust-lang.org/x"
    md = render_markdown(payload)
    assert "## Releases" in md and "(https://blog.rust-lang.org/x)" in md


async def test_agent_streaming_progress(monkeypatch):
    events, payload = await _run(monkeypatch, streaming=True)
    texts = [e.status_update.status.message.parts[0].text for e in events
             if e.HasField("status_update") and e.status_update.status.message.parts]
    assert any("OK  Hacker News" in t for t in texts)
    assert any(t.startswith("Briefing ready") for t in texts)
    assert payload["items"]


async def test_agent_sections_are_passed_through(monkeypatch):
    seen = {}

    async def capture(q, progress=None, **kw):
        seen["sections"] = q.sections
        return [], {}

    monkeypatch.setattr(news_agent, "gather_news", capture)
    app = news_agent.build_app("127.0.0.1", 9998)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:9998") as http:
        card = await A2ACardResolver(httpx_client=http, base_url="http://127.0.0.1:9998").get_agent_card()
        client = await create_client(agent=card, client_config=ClientConfig(
            streaming=False, httpx_client=http))
        req = SendMessageRequest(message=new_text_message("sections=world,politics hours=12",
                                                          role=Role.ROLE_USER))
        events = [e async for e in client.send_message(req)]
    assert seen["sections"] == ["world", "politics"]
    payload = next(d for e in events for a in e.task.artifacts for d in get_data_parts(a.parts))
    assert payload["query"]["sections"] == ["world", "politics"] and payload["items"] == []
