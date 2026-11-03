# Daily Briefing — an A2A agent (open source, world, politics)

An [A2A](https://a2a-protocol.org) agent that builds a **daily news briefing** from free, no-key
sources, in three selectable sections:

- **Open Source** — Hacker News, new & rising GitHub repos, and open source RSS feeds.
- **World** — ABC News (Australia), The Guardian (World, Australia), BBC World, Al Jazeera, DW, NPR, SBS.
- **Politics** — Guardian Australian Politics, BBC Politics, Guardian UK & US Politics, NPR Politics.

Items are de-duplicated and ranked; every item shows its source, and headlines and snippets
are shown exactly as the feed published them (no rewriting or summarising). No LLM, no API
keys. Built on `a2a-sdk` 1.3.0 (A2A protocol 1.0, JSON-RPC over HTTP).

## Files
| File | Purpose |
|---|---|
| `news_agent.py` | A2A server (default `http://127.0.0.1:9998`). Agent Card at `/.well-known/agent-card.json`; skills `latest_open_source_news`, `world_news`, `politics_news`, `daily_briefing`; streams per-source progress. |
| `news_core.py` | Sources, input parsing, filters, de-duplication, ranking. |
| `render.py` | Deterministic Markdown rendering (shared by server and client). |
| `briefing_client.py` | A2A client: fetches the card, calls the agent, prints Markdown. `--sections`, `--stream`, `--save`. |
| `run_daily.sh` | Start the agent → save `briefings/briefing-YYYY-MM-DD.md` → stop it (for cron). |
| `tests/` | pytest suite: mocked HTTP (respx) and in-process A2A end-to-end tests. |

## Input
Free text sent as the A2A message, all parts optional:

| Example | Meaning |
|---|---|
| *(empty)* | all three sections, last 24h |
| `sections=opensource` | open source only (the original behaviour) |
| `sections=world,politics hours=12 limit=5` | chosen sections, window, items per section |
| `rust`, `AI last 48h`, `topic=kubernetes days=3` | topic/keyword filter and window |
| `world news`, `politics climate`, `open source news` | section keywords also select sections |

Section aliases: `oss`/`os`/`foss` → opensource, `international`/`global` → world, `all`.

## Output
Artifact `daily-briefing` with a JSON data part (`query`, `items[]` with `title, url, source,
published, section, theme/region, points, comments, discussion_url, summary, also_on, related,
score`, and a `sources` status block) plus a `text/markdown` part.

## Sources and ranking
| Section | Sources | Ranking signal |
|---|---|---|
| Open source | Hacker News (Algolia API, filtered for open-source signals), GitHub search (repos created in last 7 days by stars, unauthenticated: 1 request/briefing), RSS: LWN.net, GitHub Blog, Linux Foundation, Opensource.com, The New Stack (keyword-filtered), Phoronix; r/opensource attempted (Reddit currently returns 403 without auth) | 0.65 × popularity (log HN points / stars; RSS baseline) + 0.35 × recency + cross-source bonus |
| World | ABC News Just In & World, Guardian Australia & World, BBC World, Al Jazeera, DW, NPR World, SBS News World | 0.7 × recency + 0.12 per *other outlet* with the same story (max 3) + small Australia bonus |
| Politics | Guardian Australian Politics, BBC Politics, Guardian UK Politics, Guardian US Politics, NPR Politics | same as World |

- Every source is fetched concurrently with its own timeout; a failing source only shows up
  as ❌ in the sources footer.
- De-duplication merges identical URLs / near-identical titles ("also on …"). For news, other
  outlets carrying a similar story are listed as "similar coverage" and raise the rank.
- Balance: open source caps any one source at ~30% of the section; world/politics cap each
  outlet+region at ~25% and reserve ~3/8 of slots for Australian stories (Australian
  editions, or headlines/snippets mentioning Australia) when available.
- World/politics skip opinion columns, podcasts, and lifestyle/culture/sport URLs; a story in
  a politics feed is shown under Politics only.
- ABC's dedicated politics/WA topic feeds currently return HTTP 500, so they are not used.

## Setup
From the repo root (one venv for the whole repo):
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cd briefing
```

## Run
```bash
python news_agent.py                                   # terminal 1 (port 9998)

python briefing_client.py                              # terminal 2: all sections, 8 per section
python briefing_client.py --stream --save              # progress + briefing-YYYY-MM-DD.md
python briefing_client.py --sections opensource --top 10
python briefing_client.py --sections world,politics --hours 12
python briefing_client.py rust --sections opensource --hours 72
```

## Tests
```bash
python -m pytest -q
```

## Daily schedule (cron) — not installed
`run_daily.sh` starts the agent, saves the briefing in `briefings/`, then stops the agent.
To run it every day at 07:00 (machine time), add with `crontab -e`
(replace `/path/to/a2a-daily-news-report`):
```
0 7 * * * /path/to/a2a-daily-news-report/briefing/run_daily.sh >> /path/to/a2a-daily-news-report/briefing/logs/cron.log 2>&1
```
Pass client options after the script, e.g. `run_daily.sh --sections opensource`.
