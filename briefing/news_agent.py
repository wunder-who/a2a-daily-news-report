"""A2A server: Daily Briefing agent — open source, world and politics news (a2a-sdk 1.3.0).

Input: optional free text, e.g. "", "rust", "AI last 48h", "topic=kubernetes hours=72 limit=20",
"sections=opensource,world,politics" (default: all three; "sections=opensource" = open source only).
Output artifact `open-source-briefing` with two parts:
  - data part (application/json): {"query", "generated_at", "items": [...], "sources": {...}}
  - text part (text/markdown): a ready-to-read briefing
Streaming: one status update per source as it finishes.

Run: python news_agent.py [--port 9998]
"""
import argparse
from datetime import datetime, timezone

import uvicorn
from starlette.applications import Starlette

from a2a.helpers import (
    get_message_text,
    new_data_part,
    new_task_from_user_message,
    new_text_message,
    new_text_part,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill, TaskState

from news_core import gather_news, parse_query
from render import render_markdown


class NewsExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        if not task:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        upd = TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id)

        q = parse_query(get_message_text(context.message) or "")
        await upd.update_status(
            state=TaskState.TASK_STATE_WORKING,
            message=new_text_message(
                f"Fetching news (sections={','.join(q.sections)}, topic={q.topic or 'any'}, last {q.hours}h)..."),
        )

        async def progress(name: str, st: dict) -> None:
            msg = (f"OK  {name}: {st['fetched']} items ({st['seconds']}s)" if st["ok"]
                   else f"ERR {name}: {st['error']}")
            await upd.update_status(state=TaskState.TASK_STATE_WORKING,
                                    message=new_text_message(msg))

        items, status = await gather_news(q, progress)
        payload = {
            "query": {"topic": q.topic, "hours": q.hours, "limit": q.limit, "sections": q.sections},
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "items": [i.to_dict() for i in items],
            "sources": status,
        }
        await upd.add_artifact(
            name="daily-briefing",
            parts=[new_data_part(payload, media_type="application/json"),
                   new_text_part(text=render_markdown(payload), media_type="text/markdown")],
        )
        ok = sum(1 for s in status.values() if s["ok"])
        await upd.update_status(
            state=TaskState.TASK_STATE_COMPLETED,
            message=new_text_message(
                f"Briefing ready: {len(items)} items from {ok}/{len(status)} sources."),
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("Cancel is not supported.")


def build_card(host: str, port: int) -> AgentCard:
    return AgentCard(
        name="Daily Briefing Agent",
        description=("Daily news briefing in three sections. Open source: Hacker News, GitHub, and RSS "
                     "(LWN, GitHub Blog, Linux Foundation, Opensource.com, The New Stack, Phoronix). "
                     "World & politics: RSS from ABC News (Australia), The Guardian, BBC, Al Jazeera, "
                     "DW, NPR and SBS. De-duplicated and ranked; headlines shown as published, "
                     "with the source on every item. No API keys or LLM."),
        version="0.2.0",
        default_input_modes=["text/plain"],
        default_output_modes=["application/json", "text/markdown"],
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[AgentInterface(protocol_binding="JSONRPC",
                                             url=f"http://{host}:{port}", protocol_version="1.0")],
        skills=[
            AgentSkill(
                id="latest_open_source_news",
                name="Latest open source news",
                description=("Ranked, de-duplicated list of recent open source news (title, URL, "
                             "source, time). Send 'sections=opensource' plus an optional topic and "
                             "window, e.g. 'sections=opensource rust', 'open source AI last 48h'."),
                input_modes=["text/plain"],
                output_modes=["application/json", "text/markdown"],
                tags=["news", "open-source", "github", "hacker-news", "rss"],
                examples=["sections=opensource", "sections=opensource rust",
                          "open source news topic=kubernetes hours=72"],
            ),
            AgentSkill(
                id="world_news",
                name="World news",
                description=("Latest world headlines from ABC News (Australia), Guardian World & "
                             "Australia, BBC World, Al Jazeera, DW, NPR and SBS, ranked by recency and "
                             "multi-outlet coverage, split into Australia / International."),
                input_modes=["text/plain"],
                output_modes=["application/json", "text/markdown"],
                tags=["news", "world", "australia", "rss"],
                examples=["sections=world", "world news last 12h", "sections=world topic=climate"],
            ),
            AgentSkill(
                id="politics_news",
                name="Politics news",
                description=("Latest political headlines: Guardian Australian Politics, BBC Politics, "
                             "Guardian UK & US Politics, NPR Politics. Headlines as published."),
                input_modes=["text/plain"],
                output_modes=["application/json", "text/markdown"],
                tags=["news", "politics", "australia", "rss"],
                examples=["sections=politics", "politics hours=48"],
            ),
            AgentSkill(
                id="daily_briefing",
                name="Daily briefing",
                description=("Markdown daily briefing with sections Open Source, World and Politics "
                             "(default: all three; choose with 'sections=opensource,world,politics')."),
                input_modes=["text/plain"],
                output_modes=["application/json", "text/markdown"],
                tags=["briefing", "daily", "markdown"],
                examples=["", "sections=opensource,world,politics", "this week"],
            ),
        ],
    )


def build_app(host: str = "127.0.0.1", port: int = 9998) -> Starlette:
    card = build_card(host, port)
    handler = DefaultRequestHandler(agent_executor=NewsExecutor(),
                                    task_store=InMemoryTaskStore(), agent_card=card)
    routes = [*create_agent_card_routes(card), *create_jsonrpc_routes(handler, "/")]
    return Starlette(routes=routes)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9998)
    a = ap.parse_args()
    uvicorn.run(build_app(a.host, a.port), host=a.host, port=a.port)
