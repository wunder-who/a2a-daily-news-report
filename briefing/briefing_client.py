"""A2A client: fetch the agent card, request a briefing, print Markdown.

Usage:
  python briefing_client.py                       # all sections, last 24h, top 8 per section
  python briefing_client.py --sections opensource # open source only (previous behaviour)
  python briefing_client.py --sections world,politics
  python briefing_client.py "rust" --hours 72     # topic + window
  python briefing_client.py --stream              # show per-source progress events
  python briefing_client.py --save                # also write briefing-YYYY-MM-DD.md
"""
import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

import httpx

from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import get_data_parts, new_text_message
from a2a.types import Role, SendMessageRequest

from render import render_markdown


def _artifact_payload(artifacts):
    for art in artifacts:
        for data in get_data_parts(art.parts):
            if isinstance(data, dict) and "items" in data:
                return data
    return None


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("topic", nargs="*", help="optional topic/keyword, e.g. rust, AI")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--top", type=int, default=8, help="items per section")
    ap.add_argument("--sections", default="opensource,world,politics",
                    help="comma list of opensource,world,politics (default: all)")
    ap.add_argument("--url", default="http://127.0.0.1:9998")
    ap.add_argument("--stream", action="store_true", help="stream progress events")
    ap.add_argument("--save", action="store_true", help="write briefing-YYYY-MM-DD.md")
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--show-card", action="store_true", help="print the full agent card")
    a = ap.parse_args()

    async with httpx.AsyncClient(timeout=10) as http:
        card = await A2ACardResolver(httpx_client=http, base_url=a.url).get_agent_card()
    print(f"<!-- Agent: {card.name} v{card.version} | skills: "
          f"{', '.join(s.id for s in card.skills)} | streaming={card.capabilities.streaming} -->",
          file=sys.stderr)
    if a.show_card:
        from google.protobuf.json_format import MessageToJson
        print(MessageToJson(card, indent=2), file=sys.stderr)

    topic = " ".join(a.topic)
    text = f"sections={a.sections} hours={a.hours} limit={a.top} {topic}".strip()
    client = await create_client(agent=card, client_config=ClientConfig(
        streaming=a.stream, httpx_client=httpx.AsyncClient(timeout=120)))
    req = SendMessageRequest(message=new_text_message(text, role=Role.ROLE_USER))

    payload, artifacts, task = None, [], None
    async for resp in client.send_message(req):
        if resp.HasField("task"):
            task = resp.task
            artifacts.extend(task.artifacts)
        elif resp.HasField("status_update"):
            st = resp.status_update.status
            if a.stream and st.message.parts:
                print(f"[progress] {st.message.parts[0].text}", file=sys.stderr)
        elif resp.HasField("artifact_update"):
            artifacts.append(resp.artifact_update.artifact)
        elif resp.HasField("message"):
            print(resp.message, file=sys.stderr)
    await client.close()

    payload = _artifact_payload(artifacts)
    if payload is None:
        print("No briefing artifact received.", file=sys.stderr)
        return 1
    md = render_markdown(payload, top=a.top)
    print(md)
    if a.save:
        path = Path(a.out_dir) / f"briefing-{datetime.now():%Y-%m-%d}.md"
        path.write_text(md, encoding="utf-8")
        print(f"Saved {path.resolve()}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
