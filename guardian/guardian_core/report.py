"""Report generation (Anthropic API), output enforcement, Telegram delivery."""

from __future__ import annotations

import html
import json
import logging
import re

import requests

from .analysis import SEVERITY

log = logging.getLogger("guardian.report")


# ---------------------------------------------------------------- generation

def build_user_message(data: dict, session: str) -> str:
    big_movers = data.get("big_movers") or []
    searches = ['"stock market today"']
    searches += [f'"{t} stock news"' for t in big_movers]
    kind = ("PRE-MARKET report (60 minutes before the U.S. open)" if session == "premarket"
            else "END-OF-DAY stop review (after the U.S. close): focus on Steps 4, 5 and 8")
    return (
        f"Write today's Guardian {kind}.\n\n"
        f"Web searches to run: {', '.join(searches)}. "
        "If KOSPI moved 5% or more, also search for a KOSPI sidecar or circuit breaker today. "
        "Also search the U.S. economic calendar for the next 5 trading days if the DATA block "
        "does not already cover CPI, jobs and FOMC. Keep searches to what is listed.\n\n"
        "Rules for numbers: use only the DATA block below or cited search results. "
        "stop_audit values are final. Do not exceed the line limit.\n\n"
        f"<DATA>\n{json.dumps(data, indent=1, default=str)}\n</DATA>"
    )


def generate(system_prompt: str, user_msg: str, llm: dict) -> str:
    import anthropic

    client = anthropic.Anthropic()
    tools = [{"type": "web_search_20260209", "name": "web_search",
              "max_uses": llm.get("web_search_max_uses", 5)}]
    messages = [{"role": "user", "content": user_msg}]
    so_far: list = []  # assistant content across pause_turn continuations
    for _ in range(4):  # pause_turn continuation for long server-tool turns
        with client.beta.messages.stream(
            model=llm["model"],
            max_tokens=llm.get("max_tokens", 16000),
            system=system_prompt,
            thinking={"type": "adaptive"},
            output_config={"effort": llm.get("effort", "high")},
            tools=tools,
            messages=messages,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            msg = stream.get_final_message()
        if msg.stop_reason == "refusal":
            raise RuntimeError(f"model declined the request: {getattr(msg, 'stop_details', None)}")
        so_far = so_far + list(msg.content)
        if msg.stop_reason == "pause_turn":
            messages = [messages[0], {"role": "assistant", "content": so_far}]
            continue
        if msg.stop_reason == "max_tokens":
            log.warning("report hit max_tokens; output may be truncated")
        # Keep only the text after the last search, dropping "let me search" preamble.
        last_tool = max((i for i, b in enumerate(so_far)
                         if b.type == "server_tool_use" or b.type.endswith("tool_result")), default=-1)
        text = "".join(b.text for b in so_far[last_tool + 1:] if b.type == "text").strip()
        if not text:
            raise RuntimeError(f"model returned no text (stop_reason={msg.stop_reason})")
        log.info("tokens in=%s out=%s", msg.usage.input_tokens, msg.usage.output_tokens)
        return text
    raise RuntimeError("model kept pausing; giving up")


# ---------------------------------------------------------------- enforcement

STATUS_RE = re.compile(r"\b(GREEN|YELLOW|RED)\b")


def enforce(text: str, floor: str, floor_reasons: list[str], closing: list[str], max_lines: int) -> tuple[str, list[str]]:
    """Apply the format rules the code can guarantee. Returns (text, notes)."""
    notes = []
    if "\u2014" in text:
        text = text.replace(" \u2014 ", ", ").replace("\u2014", ", ")
        notes.append("replaced em dashes")
    text = re.sub(r"^```(?:markdown)?\s*\n|\n```\s*$", "", text.strip())

    lines = text.splitlines()
    first = next((i for i, l in enumerate(lines) if l.strip()), 0)
    m = STATUS_RE.search(lines[first]) if lines else None
    reported = m.group(1) if m else None
    if reported is None or SEVERITY[reported] < SEVERITY[floor]:
        why = floor_reasons[0] if floor_reasons else "status set by rule"
        new = f"**{floor}**: {why[0].upper() + why[1:]}."
        if reported:
            notes.append(f"status raised from {reported} to {floor} by rule")
            lines[first] = new
        else:
            lines.insert(0, new)
            notes.append("status line was missing")

    body = "\n".join(lines).rstrip()
    for c in closing:
        if c not in body:
            body += "\n" + c
            notes.append("added closing line")
    n = len(body.splitlines())
    if n > max_lines:
        notes.append(f"report is {n} lines, over the {max_lines}-line target")
    return body + "\n", notes


# ---------------------------------------------------------------- telegram

def _table_to_pre(rows: list[str]) -> str:
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows
             if not re.fullmatch(r"\s*\|?[\s:\-|]+\|?\s*", r)]
    if not cells:
        return ""
    width = max(len(r) for r in cells)
    cells = [r + [""] * (width - len(r)) for r in cells]
    w = [max(len(re.sub(r"\*\*", "", r[i])) for r in cells) for i in range(width)]
    out = [" ".join(re.sub(r"\*\*", "", c).ljust(w[i]) for i, c in enumerate(r)).rstrip() for r in cells]
    return "<pre>" + html.escape("\n".join(out)) + "</pre>"


def _inline(s: str) -> str:
    s = html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return s


def markdown_to_telegram(md: str) -> list[str]:
    """Telegram HTML has no tables: render them as monospace blocks."""
    blocks, table = [], []
    for line in md.splitlines():
        if line.strip().startswith("|"):
            table.append(line)
            continue
        if table:
            blocks.append(_table_to_pre(table))
            table = []
        h = re.match(r"^#{1,6}\s+(.*)", line)
        if h:
            blocks.append(f"<b>{_inline(h.group(1))}</b>")
        else:
            blocks.append(_inline(line))
    if table:
        blocks.append(_table_to_pre(table))
    # Chunk under Telegram's 4096 limit without splitting a block.
    chunks, cur = [], ""
    for b in blocks:
        if len(cur) + len(b) + 1 > 3900 and cur:
            chunks.append(cur)
            cur = ""
        cur += b + "\n"
    if cur.strip():
        chunks.append(cur)
    return chunks


def send_telegram(token: str, chat_id: str, md: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chunk in markdown_to_telegram(md):
        r = requests.post(url, json={"chat_id": chat_id, "text": chunk, "parse_mode": "HTML",
                                     "disable_web_page_preview": True}, timeout=30)
        if r.status_code == 400:  # malformed HTML: fall back to plain text
            log.warning("telegram HTML rejected (%s); sending plain text", r.text[:200])
            r = requests.post(url, json={"chat_id": chat_id, "text": re.sub(r"<[^>]+>", "", html.unescape(chunk)),
                                         "disable_web_page_preview": True}, timeout=30)
        r.raise_for_status()


def send_telegram_document(token: str, chat_id: str, filename: str, md: str) -> None:
    """The dated Markdown copy, as an attachment Oleg keeps in the chat."""
    r = requests.post(f"https://api.telegram.org/bot{token}/sendDocument",
                      data={"chat_id": chat_id},
                      files={"document": (filename, md.encode(), "text/markdown")}, timeout=60)
    r.raise_for_status()
