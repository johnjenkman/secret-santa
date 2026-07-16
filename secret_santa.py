#!/usr/bin/env python3
"""Secret Santa — generate gift assignments with exclusion constraints.
       🌟
      /🌲\\
     /🌲🌲\\
    /🌲🌲🌲\\
       ||

Targets Python 3.14+. It leans on a few of the newer language features.

Usage:
    python secret_santa.py participants.json
    python secret_santa.py participants.json --format json
    python secret_santa.py participants.json --per-person ./messages
"""

import argparse
import html
import io
import json
import ntpath
import os
import random
import re
import sys
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from string.templatelib import Interpolation, Template
from typing import Self, TypeIs

type Assignment = dict[Participant, Participant]  # giver -> receiver


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class SecretSantaError(Exception):
    """Base class for expected, user-facing errors."""


class ConfigError(SecretSantaError):
    """The participant configuration was malformed or inconsistent."""


class NoSolutionError(SecretSantaError):
    """No assignment can satisfy every exclusion constraint."""


class OutputError(SecretSantaError):
    """Writing the result to disk failed (permissions, disk full, ...)."""


# --------------------------------------------------------------------------- #
# Domain model
# --------------------------------------------------------------------------- #
def _is_str_list(value: object) -> TypeIs[list[str]]:
    """Narrow decoded JSON to a list of strings (a bare ``all(isinstance(...))`` does not)."""
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


@dataclass(frozen=True, slots=True)
class Participant:
    """One person in the exchange.

    ``excludes`` holds the names this person must *not* be assigned to give a
    gift to (e.g. a partner, or someone they gifted last year).
    """

    name: str
    email: str | None = None
    excludes: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_dict(cls, data: object) -> Self:
        """Build a Participant from decoded JSON, validating as we go."""
        match data:
            case {"name": str(name), **rest}:
                if unknown := (rest.keys() - {"email", "excludes"}):
                    raise ConfigError(
                        f"unknown key(s) for {name!r}: {', '.join(sorted(map(str, unknown)))} "
                        f"(allowed: name, email, excludes)"
                    )
                email = rest.get("email")
                excludes = rest.get("excludes", [])
            case {"name": _}:
                raise ConfigError(f"'name' must be a string: {data!r}")
            case _:
                raise ConfigError(f"each participant must be an object with a 'name': {data!r}")

        if not name.strip():
            raise ConfigError(f"'name' must not be blank: {data!r}")
        if email is not None and not isinstance(email, str):
            raise ConfigError(f"'email' must be a string or omitted for {name!r}")
        if not _is_str_list(excludes):
            raise ConfigError(f"'excludes' must be a list of names for {name!r}")

        return cls(name=name, email=email, excludes=frozenset(excludes))


# --------------------------------------------------------------------------- #
# The draw
# --------------------------------------------------------------------------- #
def _permitted(giver: Participant, receiver: Participant) -> bool:
    """A giver may draw a receiver unless it's themselves or an excluded name."""
    return giver != receiver and receiver.name not in giver.excludes


def draw(participants: Sequence[Participant], *, rng: random.Random | None = None) -> Assignment:
    """Return a random valid derangement mapping each giver to a receiver.

    Prefers a *single cycle* — one continuous chain that closes back on itself
    so there are no reciprocal "you drew me, I drew you" pairs. If exclusions
    make a single cycle impossible (or too expensive to find), it falls back to
    an arbitrary valid derangement rather than failing, so any solvable group
    still gets an assignment. It only raises when no valid assignment exists at
    all, unlike the naive "shuffle and retry" trick that can loop forever.

    ``rng`` defaults to :class:`random.SystemRandom`, so draws are unpredictable
    and can't be reconstructed from a seed. Pass a seeded ``random.Random`` when
    you need reproducibility (tests).
    """
    if len(participants) < 2:
        raise NoSolutionError("need at least two participants")

    rng = rng or random.SystemRandom()

    # Fast path: with no exclusions, a shuffled rotation is a single N-cycle
    # (everyone gives to the next person, the last wraps to the first) — a
    # guaranteed derangement in O(N), no recursion, so any group size is fine.
    if all(not p.excludes for p in participants):
        order = list(participants)
        rng.shuffle(order)
        return {giver: order[(i + 1) % len(order)] for i, giver in enumerate(order)}

    # Exclusions in play: prefer a single cycle, but never fail a solvable group.
    return _draw_single_cycle(participants, rng) or _draw_derangement(participants, rng)


def _draw_single_cycle(
    participants: Sequence[Participant],
    rng: random.Random,
    *,
    max_steps: int | None = None,
) -> Assignment | None:
    """Try to build one Hamiltonian cycle honouring every exclusion.

    Grows a single chain that visits everyone exactly once and closes back to
    the start, so the result has no reciprocal pairs or small closed loops.

    Returns the assignment, or ``None`` if no single cycle exists under the
    exclusions — or if none is found within ``max_steps``. Finding a Hamiltonian
    cycle is NP-complete, so the search is capped and the caller falls back to a
    general derangement rather than risking exponential blow-up.
    """
    givers = list(participants)
    rng.shuffle(givers)
    if max_steps is None:
        max_steps = 100_000 + 1_000 * len(givers)

    start = givers[0]
    path: list[Participant] = [start]
    in_path: set[Participant] = {start}

    def options(tail: Participant) -> Iterator[Participant]:
        candidates = [p for p in givers if p not in in_path and _permitted(tail, p)]
        rng.shuffle(candidates)
        return iter(candidates)

    stack: list[Iterator[Participant]] = [options(start)]
    steps = 0
    while stack:
        steps += 1
        if steps > max_steps:
            return None  # give up; caller falls back to the general solver

        receiver = next(stack[-1], None)
        if receiver is None:  # dead end -> backtrack
            stack.pop()
            if len(path) > 1:  # never pop the fixed start node
                in_path.discard(path.pop())
            continue

        path.append(receiver)
        in_path.add(receiver)
        if len(path) == len(givers):  # full chain: can the tail close to the start?
            if _permitted(path[-1], path[0]):
                cycle = {path[i]: path[i + 1] for i in range(len(path) - 1)}
                cycle[path[-1]] = path[0]
                return cycle
            in_path.discard(path.pop())  # can't close here; try another tail
            continue
        stack.append(options(receiver))

    return None  # search exhausted: no single cycle exists under these exclusions


def _draw_derangement(participants: Sequence[Participant], rng: random.Random) -> Assignment:
    """Fallback: any valid derangement (may contain reciprocal pairs).

    Uses an explicit stack rather than recursion, so search depth is bounded by
    the heap instead of Python's recursion limit — a 1,500-person draw won't
    raise ``RecursionError``. (Backtracking is exponential in the worst case for
    adversarial exclusion sets, but fast for realistic ones; the fast path in
    ``draw`` already covers the unconstrained big-N case in linear time.)
    """
    givers = list(participants)
    rng.shuffle(givers)

    def options(index: int) -> Iterator[Participant]:
        giver = givers[index]
        candidates = [p for p in participants if p not in taken and _permitted(giver, p)]
        rng.shuffle(candidates)
        return iter(candidates)

    assignment: Assignment = {}
    taken: set[Participant] = set()
    stack: list[Iterator[Participant]] = [options(0)]

    while stack:
        index = len(stack) - 1
        giver = givers[index]
        # Returning to a level means its previous pick failed: undo it first.
        if giver in assignment:
            taken.discard(assignment.pop(giver))

        receiver = next(stack[-1], None)
        if receiver is None:  # candidates exhausted -> backtrack
            stack.pop()
            continue

        assignment[giver] = receiver
        taken.add(receiver)
        if index + 1 == len(givers):  # every giver placed
            return assignment
        stack.append(options(index + 1))

    raise NoSolutionError("no assignment satisfies all exclusions — relax the constraints")


# --------------------------------------------------------------------------- #
# Loading & validation
# --------------------------------------------------------------------------- #
def load_participants(path: Path) -> list[Participant]:
    """Read and validate participants from a JSON file."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path} is not valid UTF-8: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc

    if not isinstance(raw, list):
        raise ConfigError("top-level JSON must be a list of participants")

    participants = [Participant.from_dict(item) for item in raw]
    _validate(participants)
    return participants


def _validate(participants: Sequence[Participant]) -> None:
    """Reject duplicate names and exclusions that point at nobody."""
    counts = Counter(p.name for p in participants)
    duplicates = sorted(name for name, n in counts.items() if n > 1)
    if duplicates:
        raise ConfigError(f"duplicate names: {', '.join(duplicates)}")

    known = set(counts)
    for p in participants:
        if unknown := (p.excludes - known):
            raise ConfigError(f"{p.name} excludes unknown name(s): {', '.join(sorted(unknown))}")


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render(template: Template) -> str:
    """Render a t-string ``Template`` to text, HTML-escaping interpolations.

    This is the idiomatic use for template strings (PEP 750): the template keeps
    static text and interpolated *data* separate, so we can process each
    interpolation — here, escaping names before they land in an HTML email —
    while leaving the literal markup untouched.
    """
    parts: list[str] = []
    for item in template:
        match item:
            case str() as static:
                parts.append(static)
            case Interpolation() as field_:
                parts.append(html.escape(str(field_.value)))
    return "".join(parts)


def html_message(giver: Participant, receiver: Participant) -> str:
    """A festive, self-contained HTML card revealing one giver's Secret Santa match."""
    return render(
        t"<!DOCTYPE html>\n"
        t"<html lang='en'>\n"
        t"<head>\n"
        t"<meta charset='utf-8'>\n"
        t"<meta name='viewport' content='width=device-width, initial-scale=1'>\n"
        t"<title>Secret Santa</title>\n"
        t"<link rel='preconnect' href='https://fonts.googleapis.com'>\n"
        t"<link rel='preconnect' href='https://fonts.gstatic.com' crossorigin>\n"
        t"<link rel='stylesheet' "
        t"href='https://fonts.googleapis.com/css2?family=Gilda+Display&family=DM+Sans:"
        t"wght@400;500;600;700&display=swap'>\n"
        t"<style>\n"
        t"* {{ box-sizing: border-box; }}\n"
        t"html, body {{ margin: 0; padding: 0; }}\n"
        t"body {{\n"
        t"  min-height: 100vh; display: flex; align-items: center; justify-content: center;\n"
        t"  padding: 40px 20px; font-family: 'DM Sans', sans-serif;\n"
        t"  background: radial-gradient(circle at 50% 18%, #1f4535 0%,"
        t" #16342a 45%, #0e241d 100%);\n"
        t"  position: relative; overflow: hidden;\n"
        t"}}\n"
        t"@keyframes santa-snow {{\n"
        t"  0%   {{ transform: translateY(-10px) translateX(0); opacity: 0; }}\n"
        t"  10%  {{ opacity: 0.9; }}\n"
        t"  90%  {{ opacity: 0.9; }}\n"
        t"  100% {{ transform: translateY(620px) translateX(24px); opacity: 0; }}\n"
        t"}}\n"
        t".snowfall {{ position: absolute; inset: 0; overflow: hidden; pointer-events: none; }}\n"
        t".snowfall span {{\n"
        t"  position: absolute; top: -20px; color: #fff; font-size: 14px;\n"
        t"  animation: santa-snow 9s linear infinite;\n"
        t"}}\n"
        t".card {{\n"
        t"  position: relative; width: 100%; max-width: 400px;\n"
        t"  background: linear-gradient(180deg, #fdfaf3 0%, #f8f1e3 100%);\n"
        t"  border-radius: 26px; padding: 40px 34px 34px; text-align: center;\n"
        t"  box-shadow: 0 30px 70px -25px rgba(0,0,0,0.55),"
        t" 0 0 0 1px rgba(201,162,75,0.35) inset;\n"
        t"}}\n"
        t".card::before {{\n"
        t"  content: ''; position: absolute; top: 0; left: 0; right: 0; height: 20px;\n"
        t"  border-radius: 26px 26px 0 0;\n"
        t"  background: repeating-linear-gradient(45deg, #c1272d 0 14px, #fdfaf3 14px 28px,"
        t" #1e6b47 28px 42px, #fdfaf3 42px 56px);\n"
        t"}}\n"
        t".badge {{\n"
        t"  display: inline-flex; align-items: center; gap: 8px; margin-top: 6px;\n"
        t"  padding: 6px 14px; border: 1px solid rgba(201,162,75,0.5); border-radius: 999px;\n"
        t"  color: #a97b1e; font-size: 12px; font-weight: 600; letter-spacing: 0.18em;\n"
        t"  text-transform: uppercase;\n"
        t"}}\n"
        t".badge .dot {{ width: 6px; height: 6px; border-radius: 50%; }}\n"
        t".badge .dot.red {{ background: #c1272d; }}\n"
        t".badge .dot.green {{ background: #1e6b47; }}\n"
        t".greeting {{ margin: 22px 0 4px; font-size: 17px; color: #4a4038; }}\n"
        t".giver-name {{\n"
        t"  margin: 0; font-family: 'Gilda Display', serif; font-weight: 400; font-size: 30px;\n"
        t"  color: #1e3a2b; line-height: 1.15;\n"
        t"}}\n"
        t".lead-in {{ margin: 24px 0 16px; font-size: 15px; color: #6b5f54; }}\n"
        t".reveal-box {{\n"
        t"  position: relative; margin: 0 auto; padding: 30px 22px; border-radius: 18px;\n"
        t"  background: linear-gradient(160deg, #1e3a2b 0%, #163023 100%);\n"
        t"  box-shadow: 0 14px 30px -12px rgba(20,50,35,0.7); overflow: hidden;\n"
        t"}}\n"
        t"summary {{\n"
        t"  display: flex; flex-direction: column; align-items: center; gap: 12px;\n"
        t"  cursor: pointer; list-style: none; color: #fdfaf3;\n"
        t"}}\n"
        t"summary::-webkit-details-marker {{ display: none; }}\n"
        t".reveal-box[open] summary {{ display: none; }}\n"
        t".gift-emoji {{ font-size: 40px; line-height: 1; }}\n"
        t".tap-prompt {{ font-size: 15px; font-weight: 600; }}\n"
        t".tap-hint {{ font-size: 12px; color: rgba(253,250,243,0.55); }}\n"
        t".match-label {{\n"
        t"  font-size: 12px; letter-spacing: 0.22em; text-transform: uppercase; color: #c9a24b;\n"
        t"  margin-bottom: 8px;\n"
        t"}}\n"
        t".match-name {{\n"
        t"  margin: 0; font-family: 'Gilda Display', serif; font-weight: 400; font-size: 38px;\n"
        t"  line-height: 1.1; color: #fdfaf3;\n"
        t"}}\n"
        t".footer-note {{\n"
        t"  display: flex; align-items: center; justify-content: center; gap: 8px;\n"
        t"  margin: 22px 0 2px; color: #8a7d70; font-size: 13px; font-style: italic;\n"
        t"}}\n"
        t"</style>\n"
        t"</head>\n"
        t"<body>\n"
        t"<div class='snowfall'>\n"
        t"<span style='left:6%; font-size:10px; opacity:0.4; "
        t"animation-duration:8s; animation-delay:-1s;'>&#10052;</span>\n"
        t"<span style='left:18%; font-size:16px; opacity:0.7; "
        t"animation-duration:11s; animation-delay:-4s;'>&#10052;</span>\n"
        t"<span style='left:32%; font-size:12px; opacity:0.5; "
        t"animation-duration:9s; animation-delay:-2s;'>&#10052;</span>\n"
        t"<span style='left:47%; font-size:18px; opacity:0.8; "
        t"animation-duration:12s; animation-delay:-6s;'>&#10052;</span>\n"
        t"<span style='left:61%; font-size:11px; opacity:0.45; "
        t"animation-duration:8.5s; animation-delay:-3s;'>&#10052;</span>\n"
        t"<span style='left:74%; font-size:15px; opacity:0.65; "
        t"animation-duration:10s; animation-delay:-5s;'>&#10052;</span>\n"
        t"<span style='left:86%; font-size:13px; opacity:0.55; "
        t"animation-duration:9.5s; animation-delay:-1.5s;'>&#10052;</span>\n"
        t"<span style='left:94%; font-size:17px; opacity:0.75; "
        t"animation-duration:11.5s; animation-delay:-7s;'>&#10052;</span>\n"
        t"</div>\n"
        t"<div class='card'>\n"
        t"<div class='badge'>\n"
        t"<span class='dot red'></span>\n"
        t"Secret Santa\n"
        t"<span class='dot green'></span>\n"
        t"</div>\n"
        t"<p class='greeting'>Ho ho ho,</p>\n"
        t"<h2 class='giver-name'>{giver.name}</h2>\n"
        t"<p class='lead-in'>You&rsquo;re the secret gift-giver for&hellip;</p>\n"
        t"<details class='reveal-box'>\n"
        t"<summary>\n"
        t"<span class='gift-emoji'>&#127873;</span>\n"
        t"<span class='tap-prompt'>Tap to reveal</span>\n"
        t"<span class='tap-hint'>Ready when you are</span>\n"
        t"</summary>\n"
        t"<div class='match-label'>Your match</div>\n"
        t"<h1 class='match-name'>{receiver.name}</h1>\n"
        t"</details>\n"
        t"<p class='footer-note'><span>&#129323;</span> Keep it a secret until the big day</p>\n"
        t"</div>\n"
        t"</body>\n"
        t"</html>\n"
    )


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
class OutputFormat(StrEnum):
    TABLE = "table"
    JSON = "json"
    HTML = "html"


def emit(assignment: Assignment, fmt: OutputFormat) -> None:
    """Print the assignment to stdout in the requested format."""
    ordered = sorted(assignment.items(), key=lambda kv: kv[0].name)
    match fmt:
        case OutputFormat.TABLE:
            if not ordered:
                return
            width = max(len(g.name) for g, _ in ordered)
            # ANSI colors only for a real terminal; files and pipes get plain text.
            if sys.stdout.isatty():
                green, red, reset = "\033[92m", "\033[91m", "\033[0m"
            else:
                green = red = reset = ""

            print(f"\n{green}🎄 Secret Santa Assignments 🎄{reset}\n")

            for i, (giver, receiver) in enumerate(ordered):
                # Alternate colors per row!
                color = red if i % 2 == 0 else green
                print(
                    f"{color}{giver.name:<{width}} 🎁 \N{RIGHTWARDS ARROW} {receiver.name}{reset}"
                )
        case OutputFormat.JSON:
            print(json.dumps({g.name: r.name for g, r in ordered}, indent=2, ensure_ascii=False))
        case OutputFormat.HTML:
            for giver, receiver in ordered:
                print(html_message(giver, receiver))


def _slugify(name: str) -> str:
    """Reduce a name to a filesystem-safe slug.

    Keeps Unicode letters and digits — so "José" and "李雷" survive intact —
    but turns everything else, crucially path separators and dots, into hyphens.
    That alone neutralises traversal attempts like "../admin"; the caller adds a
    resolved-path check as a second line of defence.
    """
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in name.casefold())
    return re.sub(r"-{2,}", "-", cleaned).strip("-")


def write_per_person(assignment: Assignment, out_dir: Path) -> int:
    """Write one HTML message per giver so each can be shared privately."""
    out_dir = out_dir.resolve()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OutputError(f"could not create {out_dir}: {exc}") from exc

    seen: set[str] = set()
    for giver, receiver in assignment.items():
        slug = base = _slugify(giver.name) or "participant"
        suffix = 2
        # Rename on collision (one person's file must never silently overwrite
        # another's) and on Windows-reserved device names ("con", "nul", ...),
        # which can silently swallow the file even when generated elsewhere.
        while slug in seen or ntpath.isreserved(f"{slug}.html"):
            slug = f"{base}-{suffix}"
            suffix += 1
        seen.add(slug)

        target = (out_dir / f"{slug}.html").resolve()
        if not target.is_relative_to(out_dir):  # defence in depth: stay inside out_dir
            raise OutputError(f"refusing to write outside {out_dir} for name {giver.name!r}")
        try:
            target.write_text(html_message(giver, receiver), encoding="utf-8")
        except OSError as exc:
            raise OutputError(f"could not write {target}: {exc}") from exc
    return len(assignment)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="secret-santa",
        description="🎅 Generate Secret Santa assignments with exclusion support. 🦌",
    )
    parser.add_argument(
        "participants",
        type=Path,
        help="JSON file: a list of {name, email?, excludes?} objects",
    )
    parser.add_argument(
        "-f",
        "--format",
        type=OutputFormat,
        choices=list(OutputFormat),
        default=OutputFormat.TABLE,
        help="output format (default: table)",
    )
    parser.add_argument(
        "-o",
        "--per-person",
        type=Path,
        metavar="DIR",
        help="also write one HTML message per giver into DIR",
    )
    parser.add_argument(
        "--seed",
        type=int,
        help="integer seed for a reproducible draw (testing only)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # Emit UTF-8 regardless of the locale: redirected output on Windows defaults
    # to a legacy code page (e.g. cp1252) that can't encode 🎄/→, and the HTML
    # output declares charset='utf-8'. (isinstance: test harnesses swap in streams
    # that have no reconfigure().)
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")

    args = build_parser().parse_args(argv)
    rng = random.Random(args.seed) if args.seed is not None else random.SystemRandom()

    try:
        participants = load_participants(args.participants)
        assignment = draw(participants, rng=rng)
        emit(assignment, args.format)
        if args.per_person is not None:
            count = write_per_person(assignment, args.per_person)
            print(f"\nWrote {count} messages to {args.per_person}/", file=sys.stderr)
    except SecretSantaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt, BrokenPipeError:  # PEP 758: no parentheses when not binding
        # Point stdout at devnull so the interpreter's shutdown flush doesn't
        # print "Exception ignored" noise when the pipe is already closed.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        raise SystemExit(130) from None
