# Minimal A2A (Agent2Agent) demo — Python

A tiny A2A agent + client using the official `a2a-sdk` (v1.3.0, protocol 1.0).
No LLM or API keys: the agent greets you and reverses your text.

- `server.py` — agent server on http://127.0.0.1:9999 (JSON-RPC binding).
  Agent Card at `/.well-known/agent-card.json`, one skill `greet_and_reverse`, streaming enabled.
- `client.py` — fetches the Agent Card, sends a message (non-streaming), then the same message streaming, printing every event.

## Setup
```bash
cd a2a-daily-news-report/demo
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt   # or: pip install "a2a-sdk[http-server]" uvicorn httpx sse-starlette
```

## Run
```bash
# terminal 1
python server.py
# terminal 2
curl -s http://127.0.0.1:9999/.well-known/agent-card.json
python client.py "hello A2A"
```
Expected: the task completes with an artifact like
`Hello! You said 'hello A2A'. Reversed: 'A2A olleh'`; the streaming call shows
SUBMITTED -> WORKING -> artifact -> COMPLETED events.

## Notes
- The SDK API changed a lot in 1.x (protobuf-based types, `create_client`, `create_agent_card_routes`/`create_jsonrpc_routes`, `TaskState.TASK_STATE_*`). Older 0.3 tutorials (`A2AStarletteApplication`, `A2AClient`) won't match.
- Based on the official helloworld sample: https://github.com/a2aproject/a2a-samples/tree/main/samples/python/agents/helloworld
- To change the agent's behaviour, edit `respond()` in `server.py`.
