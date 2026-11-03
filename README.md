# A2A agents

Small Python projects built on the [Agent2Agent (A2A) protocol](https://a2a-protocol.org)
using the official [`a2a-sdk`](https://github.com/a2aproject/a2a-python) (v1.3.0, protocol 1.0).

| Folder | What it is | Port |
|---|---|---|
| [`demo/`](demo/) | Minimal "Hello World" A2A agent + client: Agent Card, a text reply (greets and reverses your text), and a streaming call. | 9999 |
| [`briefing/`](briefing/) | Daily Briefing agent: open source, world and politics news from free, no-key sources (Hacker News, GitHub, RSS from ABC, Guardian, BBC, Al Jazeera, DW, NPR, SBS, LWN, Phoronix…), de-duplicated and ranked, delivered as JSON + Markdown. Includes a client, tests and a cron script. | 9998 |

No API keys or LLMs are needed for either.

## Quick start
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# Hello World demo
cd demo && python server.py &          # then: python client.py "hello A2A"

# Daily briefing
cd briefing && python news_agent.py &  # then: python briefing_client.py --stream
python -m pytest -q                    # (in briefing/) run the tests
```
See each folder's README for details.

## Note on the A2A SDK version
`a2a-sdk` 1.x changed its API substantially from 0.3 (protobuf types, `create_client`,
`create_agent_card_routes` / `create_jsonrpc_routes`, `TaskState.TASK_STATE_*`). Code here
targets 1.3.0; older tutorials using `A2AStarletteApplication` / `A2AClient` will not match.

## License
Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
