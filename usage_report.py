#!/usr/bin/env python3
"""AI coding usage report: tokens and estimated USD cost per project.

Reads local session logs of Claude Code (~/.claude/projects) and OpenAI Codex
CLI (~/.codex/sessions), merges them by working directory, and aggregates the
last N days per project and per model. Standard library only, Python 3.9+.

Usage:
    python3 usage_report.py                    # last 30 days, all projects, both tools
    python3 usage_report.py --days 7 --models  # per-model breakdown
    python3 usage_report.py --source codex     # only Codex
    python3 usage_report.py --csv out.csv --json out.json
    python3 usage_report.py --pricing prices.json

Pricing file format (USD per million tokens, matched by model-id prefix):
    {"claude-sonnet-5-5": {"input": 3, "output": 15}}
Cache write (5 min) defaults to 1.25x input, 1 h to 2x input, cache read to
0.1x input; override with "cache_write_5m", "cache_write_1h", "cache_read".

Prices follow the GitHub Copilot model pricing page (see DEFAULT_PRICING).
Costs are list-price estimates, not billing. Models without a price entry are
reported with tokens only and flagged; add them via --pricing.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

# USD per million tokens, GitHub Copilot list prices (Default context tier):
# https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing
# Longest matching prefix wins. Long-context tiers and Opus fast mode cannot be
# told apart in the logs and are priced at the Default rate. Extend or override
# with --pricing.
DEFAULT_PRICING: dict[str, dict[str, float]] = {
    # Anthropic
    "claude-haiku-4-5": {"input": 1.0, "cache_read": 0.10, "cache_write_5m": 1.25, "output": 5.0},
    "claude-sonnet-4": {"input": 3.0, "cache_read": 0.30, "cache_write_5m": 3.75, "output": 15.0},
    "claude-sonnet-4-6": {"input": 3.0, "cache_read": 0.30, "cache_write_5m": 3.75, "output": 15.0},
    "claude-opus-4-7": {"input": 5.0, "cache_read": 0.50, "cache_write_5m": 6.25, "output": 25.0},
    "claude-opus-4-8": {"input": 5.0, "cache_read": 0.50, "cache_write_5m": 6.25, "output": 25.0},
    "claude-opus-5": {"input": 5.0, "cache_read": 0.50, "cache_write_5m": 6.25, "output": 25.0},
    "claude-opus-5-5": {"input": 4.0, "cache_read": 0.20, "cache_write_5m": 5.0, "output": 20.0},
    "claude-sonnet-5": {"input": 2.0, "cache_read": 0.20, "cache_write_5m": 2.5, "output": 10.0},
    "claude-sonnet-5-5": {"input": 2.0, "cache_read": 0.20, "cache_write_5m": 2.5, "output": 10.0},
    "claude-fable-5": {"input": 10.0, "cache_read": 1.0, "cache_write_5m": 12.5, "output": 50.0},
    "claude-fable-5-1": {"input": 10.0, "cache_read": 0.25, "cache_write_5m": 12.5, "output": 50.0},
    # OpenAI
    "gpt-5-mini": {"input": 0.25, "cache_read": 0.025, "output": 2.0},
    "gpt-5.3-codex": {"input": 1.75, "cache_read": 0.175, "output": 14.0},
    "gpt-5.4": {"input": 2.5, "cache_read": 0.25, "output": 15.0},
    "gpt-5.4-mini": {"input": 0.75, "cache_read": 0.075, "output": 4.5},
    "gpt-5.4-nano": {"input": 0.20, "cache_read": 0.02, "output": 1.25},
    "gpt-5.5": {"input": 5.0, "cache_read": 0.50, "output": 30.0},
    "gpt-5.6-luna": {"input": 0.20, "cache_read": 0.02, "output": 1.2},
    "gpt-5.6-sol": {"input": 4.0, "cache_read": 0.40, "output": 20.0},
    "gpt-5.6-terra": {"input": 2.0, "cache_read": 0.20, "output": 12.0},
    "gpt-6-astra": {"input": 10.0, "cache_read": 1.0, "output": 50.0},
    "gpt-6-luna": {"input": 0.10, "cache_read": 0.01, "output": 0.5},
    "gpt-6-sol": {"input": 2.0, "cache_read": 0.20, "output": 10.0},
    "gpt-6.1-sol": {"input": 2.0, "cache_read": 0.10, "output": 10.0},
    # Not on the Copilot list: legacy vendor list prices as fallback.
    "claude-opus-4-5": {"input": 5.0, "output": 25.0},
    "claude-opus-4-1": {"input": 15.0, "output": 75.0},
    "claude-opus-4": {"input": 15.0, "output": 75.0},
    "claude-3-7-sonnet": {"input": 3.0, "output": 15.0},
    "claude-3-5-haiku": {"input": 0.8, "output": 4.0},
    "gpt-5-nano": {"input": 0.05, "output": 0.4, "cache_read": 0.005},
    "gpt-5": {"input": 1.25, "output": 10.0, "cache_read": 0.125},
}

CODEX_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens")


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0
    cost: float = 0.0
    messages: int = 0
    unpriced_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.input += other.input
        self.output += other.output
        self.cache_write_5m += other.cache_write_5m
        self.cache_write_1h += other.cache_write_1h
        self.cache_read += other.cache_read
        self.cost += other.cost
        self.messages += other.messages
        self.unpriced_tokens += other.unpriced_tokens

    @property
    def total(self) -> int:
        return (self.input + self.output + self.cache_write_5m
                + self.cache_write_1h + self.cache_read)


@dataclass
class Project:
    name: str
    usage: Usage = field(default_factory=Usage)
    models: dict[str, Usage] = field(default_factory=lambda: defaultdict(Usage))
    sessions: set[str] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)


class Ledger:
    """Aggregates priced usage per project and model."""

    def __init__(self, pricing: dict[str, dict[str, float]]) -> None:
        self.pricing = pricing
        self.projects: dict[str, Project] = {}
        self.unpriced_models: set[str] = set()

    def record(self, label: str, source: str, session: str, model: str, u: Usage) -> None:
        price = find_price(model, self.pricing)
        if price is None:
            self.unpriced_models.add(model)
            u.unpriced_tokens = u.total
        else:
            u.cost = (u.input * price["input"]
                      + u.output * price["output"]
                      + u.cache_write_5m * price["cache_write_5m"]
                      + u.cache_write_1h * price["cache_write_1h"]
                      + u.cache_read * price["cache_read"]) / 1_000_000
        proj = self.projects.setdefault(label, Project(label))
        proj.usage.add(u)
        proj.models[model].add(u)
        proj.sessions.add(f"{source}:{session}")
        proj.sources.add(source)


def parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def find_price(model: str, table: dict[str, dict[str, float]]) -> dict[str, float] | None:
    best = None
    for prefix in table:
        if model.startswith(prefix) and (best is None or len(prefix) > len(best)):
            best = prefix
    if best is None:
        return None
    p = dict(table[best])
    p.setdefault("cache_write_5m", p["input"] * 1.25)
    p.setdefault("cache_write_1h", p["input"] * 2.0)
    p.setdefault("cache_read", p["input"] * 0.1)
    return p


def existing_dirs(candidates: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for c in candidates:
        if c.is_dir() and c.resolve() not in seen:
            seen.add(c.resolve())
            out.append(c)
    return out


def claude_roots(cli: list[str]) -> list[Path]:
    if cli:
        return existing_dirs([Path(r).expanduser() for r in cli])
    cands: list[Path] = []
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        cands += [Path(p).expanduser() / "projects" for p in env.split(",") if p]
    cands += [Path.home() / ".claude" / "projects",
              Path.home() / ".config" / "claude" / "projects"]
    return existing_dirs(cands)


def codex_roots(cli: list[str]) -> list[Path]:
    if cli:
        return existing_dirs([Path(r).expanduser() for r in cli])
    home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    return existing_dirs([home / "sessions", home / "archived_sessions"])


def iter_lines(path: Path, needles: tuple[str, ...]) -> Iterator[dict]:
    """Yield parsed JSON objects for lines containing any needle (cheap pre-filter)."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not any(n in line for n in needles):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError as exc:
        print(f"warn: cannot read {path}: {exc}", file=sys.stderr)


def project_label(fallback: str, cwd: str | None) -> str:
    label = cwd or fallback
    home = str(Path.home())
    if cwd and (cwd == home or cwd.startswith(home + os.sep)):
        label = "~" + cwd[len(home):]
    return label


def stale(path: Path, since: datetime) -> bool:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < since


def collect_claude(roots: list[Path], since: datetime, ledger: Ledger) -> None:
    seen: set[str] = set()
    for root in roots:
        for proj_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            files = [f for f in sorted(proj_dir.rglob("*.jsonl")) if not stale(f, since)]
            if not files:
                continue
            cwd_hint: str | None = None
            batch: list[tuple[dict, str]] = []
            for f in files:
                for e in iter_lines(f, ('"usage"', '"cwd"')):
                    if cwd_hint is None and isinstance(e.get("cwd"), str):
                        cwd_hint = e["cwd"]
                    batch.append((e, f.stem))
            label = project_label(proj_dir.name, cwd_hint)

            for e, session in batch:
                msg = e.get("message")
                if e.get("type") != "assistant" or not isinstance(msg, dict):
                    continue
                usage, model = msg.get("usage"), msg.get("model")
                if not isinstance(usage, dict) or not model or model == "<synthetic>":
                    continue
                ts = parse_ts(e.get("timestamp"))
                if ts is None or ts < since:
                    continue
                if msg.get("id") and e.get("requestId"):
                    key = f"{msg['id']}:{e['requestId']}"
                    if key in seen:
                        continue
                    seen.add(key)

                cc = usage.get("cache_creation") or {}
                w5 = int(cc.get("ephemeral_5m_input_tokens") or 0)
                w1 = int(cc.get("ephemeral_1h_input_tokens") or 0)
                if not cc:
                    w5 = int(usage.get("cache_creation_input_tokens") or 0)
                ledger.record(label, "cc", session, model, Usage(
                    input=int(usage.get("input_tokens") or 0),
                    output=int(usage.get("output_tokens") or 0),
                    cache_write_5m=w5, cache_write_1h=w1,
                    cache_read=int(usage.get("cache_read_input_tokens") or 0),
                    messages=1))


def collect_codex(roots: list[Path], since: datetime, ledger: Ledger) -> None:
    """Codex logs cumulative token totals per session; usage is the delta between events.

    input_tokens in Codex includes cached_input_tokens, and output_tokens already
    includes reasoning tokens, so they are split/kept accordingly.
    """
    needles = ('"token_count"', '"turn_context"', '"session_meta"')
    for root in roots:
        for f in sorted(root.rglob("*.jsonl")):
            if stale(f, since):
                continue
            cwd: str | None = None
            model = "codex-unknown"
            session = f.stem
            prev: tuple[int, ...] | None = None
            for e in iter_lines(f, needles):
                kind = e.get("type")
                p = e.get("payload")
                if not isinstance(p, dict):
                    continue
                if kind == "session_meta":
                    cwd = p.get("cwd") or cwd
                    session = p.get("id") or session
                elif kind == "turn_context":
                    cwd = p.get("cwd") or cwd
                    model = p.get("model") or model
                elif kind == "event_msg" and p.get("type") == "token_count":
                    info = p.get("info")
                    total = info.get("total_token_usage") if isinstance(info, dict) else None
                    if not isinstance(total, dict):
                        continue
                    cur = tuple(int(total.get(k) or 0) for k in CODEX_KEYS)
                    delta = cur if prev is None else tuple(max(0, c - q) for c, q in zip(cur, prev))
                    prev = cur
                    ts = parse_ts(e.get("timestamp"))
                    if not any(delta) or ts is None or ts < since:
                        continue
                    inp, cached, out = delta
                    cached = min(cached, inp)
                    ledger.record(project_label("(codex) unknown", cwd), "cx", session, model,
                                  Usage(input=inp - cached, output=out, cache_read=cached,
                                        messages=1))


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


PALETTE = ["1;35", "1;36", "1;32", "1;33", "1;34", "1;31"]
USE_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ


def paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


def bar(share: float, width: int = 20) -> str:
    """Horizontal bar for a 0..1 share, eighth-block resolution."""
    eighths = round(max(0.0, min(1.0, share)) * width * 8)
    full, part = divmod(eighths, 8)
    return "█" * full + ("▏▎▍▌▋▊▉"[part - 1] if part else "")


def render_box(header: list[str], rows: list[list[str]], right: set[int],
               colors: list[str] | None = None, footer: list[str] | None = None) -> str:
    """Boxed table; colors[i] is an ANSI code for row i, footer is a totals row."""
    body = rows + ([footer] if footer else [])
    widths = [max(len(str(r[i])) for r in [header] + body) for i in range(len(header))]

    def cells(r: list[str], code: str | None = None) -> str:
        parts = []
        for i, c in enumerate(r):
            t = str(c).rjust(widths[i]) if i in right else str(c).ljust(widths[i])
            parts.append(paint(t, code) if code else t)
        return "│ " + " │ ".join(parts) + " │"

    def rule(left: str, mid: str, end: str) -> str:
        return left + mid.join("─" * (w + 2) for w in widths) + end

    out = [rule("┌", "┬", "┐"), cells(header, "1;36"), rule("├", "┼", "┤")]
    out += [cells(r, colors[n] if colors else None) for n, r in enumerate(rows)]
    if footer:
        out += [rule("├", "┼", "┤"), cells(footer, "1")]
    out.append(rule("└", "┴", "┘"))
    return "\n".join(out)


def shorten(text: str, width: int = 44) -> str:
    """Keep the tail of long paths (the informative part), e.g. '…/worktrees/name'."""
    return text if len(text) <= width else "…" + text[-(width - 1):]


def cache_write(u: Usage) -> int:
    return u.cache_write_5m + u.cache_write_1h


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--days", type=int, default=30, help="window size in days (default 30)")
    ap.add_argument("--source", choices=("all", "claude", "codex"), default="all",
                    help="which tool logs to read (default all)")
    ap.add_argument("--root", action="append", default=[],
                    help="Claude Code projects directory (repeatable); default ~/.claude/projects")
    ap.add_argument("--codex-root", action="append", default=[],
                    help="Codex sessions directory (repeatable); default ~/.codex/sessions")
    ap.add_argument("--pricing", help="JSON file with extra/override prices (USD per MTok)")
    ap.add_argument("--models", action="store_true", help="show per-model breakdown")
    ap.add_argument("--csv", metavar="FILE", help="write per-project/model rows to CSV")
    ap.add_argument("--json", metavar="FILE", help="write full report to JSON")
    args = ap.parse_args()

    pricing = dict(DEFAULT_PRICING)
    if args.pricing:
        try:
            pricing.update(json.loads(Path(args.pricing).expanduser().read_text()))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"error: cannot load pricing file: {exc}", file=sys.stderr)
            return 2

    c_roots = claude_roots(args.root) if args.source in ("all", "claude") else []
    x_roots = codex_roots(args.codex_root) if args.source in ("all", "codex") else []
    if not c_roots and not x_roots:
        print("error: no Claude Code (~/.claude/projects) or Codex (~/.codex/sessions) "
              "logs found; try --root / --codex-root", file=sys.stderr)
        return 2
    print("Sources: " + ", ".join(f"{n} {r}" for n, rs in (("claude", c_roots), ("codex", x_roots))
                                  for r in rs), file=sys.stderr)

    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    ledger = Ledger(pricing)
    collect_claude(c_roots, since, ledger)
    collect_codex(x_roots, since, ledger)

    projects = {k: v for k, v in ledger.projects.items() if v.usage.messages}
    if not projects:
        print(f"No usage found in the last {args.days} days.")
        return 0

    ordered = sorted(projects.values(), key=lambda p: (p.usage.cost, p.usage.total), reverse=True)
    total = Usage()
    for p in ordered:
        total.add(p.usage)

    def usage_cells(u: Usage) -> list[str]:
        return [f"{u.messages:,}", fmt_tokens(u.input), fmt_tokens(u.output),
                fmt_tokens(cache_write(u)), fmt_tokens(u.cache_read), fmt_tokens(u.total),
                f"${u.cost:,.2f}"]

    def share_cells(u: Usage) -> list[str]:
        frac = u.cost / total.cost if total.cost else 0.0
        return [f"{frac * 100:.1f}%", bar(frac)]

    usage_header = ["Msgs", "Input", "Output", "CacheW", "CacheR", "Total", "Cost",
                    "Share", "Cost share"]
    total_share = ["100.0%" if total.cost else "0.0%", ""]

    print(f"\n{paint('AI coding usage by project', '1')}, last {args.days} days "
          f"(since {since.astimezone().strftime('%Y-%m-%d %H:%M')})\n")
    prows = [[shorten(p.name), "+".join(sorted(p.sources)), f"{len(p.sessions):,}"]
             + usage_cells(p.usage) + share_cells(p.usage) for p in ordered]
    pfoot = ["TOTAL", "", f"{sum(len(p.sessions) for p in ordered):,}"] \
        + usage_cells(total) + total_share
    print(render_box(["Project", "Src", "Sess"] + usage_header, prows, set(range(2, 11)),
                     [PALETTE[n % len(PALETTE)] for n in range(len(prows))], pfoot))

    by_model: dict[str, Usage] = defaultdict(Usage)
    for p in ordered:
        for model, u in p.models.items():
            by_model[model].add(u)
    ranked = sorted(by_model.items(), key=lambda kv: (kv[1].cost, kv[1].total), reverse=True)
    mrows = [[m + ("*" if m in ledger.unpriced_models else "")] + usage_cells(u) + share_cells(u)
             for m, u in ranked]
    print("\n" + paint("Utilization by model (all projects)", "1") + "\n")
    print(render_box(["Model"] + usage_header, mrows, set(range(1, 9)),
                     [PALETTE[n % len(PALETTE)] for n in range(len(mrows))],
                     ["TOTAL"] + usage_cells(total) + total_share))
    if ledger.unpriced_models:
        print("* no price, counted as $0")

    if args.models:
        print("\n" + paint("Per-project model breakdown", "1") + "\n")
        brows, bcolors = [], []
        for n, p in enumerate(ordered):
            for model, u in sorted(p.models.items(), key=lambda kv: kv[1].cost, reverse=True):
                brows.append([shorten(p.name), model] + usage_cells(u) + share_cells(u))
                bcolors.append(PALETTE[n % len(PALETTE)])
        print(render_box(["Project", "Model"] + usage_header, brows, set(range(2, 10)), bcolors,
                         ["TOTAL", ""] + usage_cells(total) + total_share))

    if ledger.unpriced_models:
        print(f"\nNOTE: no price for {', '.join(sorted(ledger.unpriced_models))}; those tokens "
              f"({fmt_tokens(total.unpriced_tokens)}) are counted but add $0. "
              "Supply prices via --pricing to include them.")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["project", "model", "messages", "input", "output",
                        "cache_write_5m", "cache_write_1h", "cache_read", "usd"])
            for p in ordered:
                for model, u in p.models.items():
                    w.writerow([p.name, model, u.messages, u.input, u.output,
                                u.cache_write_5m, u.cache_write_1h, u.cache_read,
                                round(u.cost, 6)])
        print(f"\nCSV written: {args.csv}")

    if args.json:
        payload = {
            "days": args.days,
            "since": since.isoformat(),
            "unpriced_models": sorted(ledger.unpriced_models),
            "total": total.__dict__,
            "models": {m: u.__dict__ for m, u in by_model.items()},
            "projects": [{"name": p.name, "sources": sorted(p.sources),
                          "sessions": len(p.sessions), **p.usage.__dict__,
                          "models": {m: u.__dict__ for m, u in p.models.items()}}
                         for p in ordered],
        }
        Path(args.json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"JSON written: {args.json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
