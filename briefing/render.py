"""Deterministic Markdown rendering of a briefing payload (shared by server and client).

Headlines and snippets are shown exactly as the source feeds provide them (no rewriting);
every item names its source.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone

THEME_ORDER = ["Security", "Releases", "Linux & Kernel", "AI & ML", "Languages & Dev Tools",
               "Licensing & Community", "Other Open Source News", "New & Rising on GitHub"]
NEWS_ORDER = ["Australia", "International"]
SECTION_TITLES = {"opensource": "Open Source", "world": "World", "politics": "Politics"}
SECTION_ORDER = ["opensource", "world", "politics"]


def _ago(iso: str, now: datetime) -> str:
    dt = datetime.fromisoformat(iso)
    h = (now - dt).total_seconds() / 3600
    if h < 1:
        return f"{max(1, int(h * 60))}m ago"
    if h < 48:
        return f"{int(h)}h ago"
    return f"{int(h // 24)}d ago"


def _local(iso: str) -> str:
    dt = datetime.fromisoformat(iso).astimezone()  # box/local zone
    return dt.strftime("%d %b %H:%M %Z")


def _snippet(text: str, n: int = 200) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0] + "…"


def _item_lines(it: dict, n: int, now: datetime) -> list[str]:
    meta = [f"**{it['source']}**", f"{_ago(it['published'], now)} ({_local(it['published'])})"]
    pts = int(it.get("points") or 0)
    if it["kind"] == "hn":
        meta.append(f"{pts} points, {int(it.get('comments') or 0)} comments")
    elif it["kind"] == "github":
        meta.append(f"★ {pts}")
    if it.get("also_on"):
        meta.append("also on " + ", ".join(it["also_on"]))
    related = [r for r in it.get("related", []) if r not in it.get("also_on", [])]
    if related:
        meta.append("similar coverage: " + ", ".join(related))
    line = f"{n}. [{it['title']}]({it['url']}) — " + " · ".join(meta)
    if it.get("discussion_url") and it["discussion_url"] != it["url"]:
        line += f" · [discussion]({it['discussion_url']})"
    out = [line]
    if it.get("section", "opensource") != "opensource" and it.get("summary"):
        out.append(f"   > {_snippet(it['summary'])}")
    return out


def _render_groups(items: list[dict], order: list[str], level: str, start: int,
                   now: datetime) -> tuple[list[str], int]:
    groups: OrderedDict[str, list] = OrderedDict((t, []) for t in order)
    for it in items:
        groups.setdefault(it.get("theme") or order[-1], []).append(it)
    lines, n = [], start
    for theme, its in groups.items():
        if not its:
            continue
        lines += [f"{level} {theme}", ""]
        for it in its:
            n += 1
            lines += _item_lines(it, n, now)
        lines.append("")
    return lines, n


def render_markdown(payload: dict, top: int | None = None) -> str:
    """`top` limits items per section."""
    now = datetime.now(timezone.utc)
    q = payload.get("query", {})
    sections = [s for s in SECTION_ORDER if s in (q.get("sections") or ["opensource"])]
    by_sec: dict[str, list] = {s: [] for s in sections}
    for it in payload.get("items", []):
        by_sec.setdefault(it.get("section", "opensource"), []).append(it)
    if top:
        by_sec = {k: v[:top] for k, v in by_sec.items()}
    total = sum(len(v) for v in by_sec.values())
    gen = datetime.fromisoformat(payload["generated_at"]).astimezone()
    single_os = sections == ["opensource"]
    title = "Open Source Daily Briefing" if single_os else "Daily Briefing"
    lines = [f"# {title} — {gen:%A %d %B %Y}", ""]
    lines.append(f"_Generated {gen:%H:%M %Z} · window: last {int(q.get('hours', 24))}h"
                 + (f" · topic: **{q['topic']}**" if q.get("topic") else "")
                 + ("" if single_os else " · sections: " + ", ".join(SECTION_TITLES[s] for s in sections))
                 + f" · {total} items_")
    lines.append("")
    n = 0
    for sec in sections:
        items = by_sec.get(sec, [])
        order = THEME_ORDER if sec == "opensource" else NEWS_ORDER
        if single_os:
            body, n = _render_groups(items, order, "##", n, now)
        else:
            lines += [f"## {SECTION_TITLES[sec]}", ""]
            body, n = _render_groups(items, order, "###", n, now)
            if not items:
                body = ["_No items in this window._", ""]
        lines += body
    src = payload.get("sources", {})
    if src:
        lines += ["---", "**Sources:**", ""]
        for sec in sections:
            names = sorted(k for k, s in src.items() if s.get("section", "opensource") == sec)
            if not names:
                continue
            parts = []
            for name in names:
                s = src[name]
                parts.append(f"{name} ✅ {int(s['fetched'])} fetched, {int(s.get('in_briefing', 0))} used"
                             if s["ok"] else f"{name} ❌ ({s['error'][:70]})")
            lines.append(f"- {SECTION_TITLES[sec]}: " + "; ".join(parts))
    return "\n".join(lines).rstrip() + "\n"
