"""Bring old conversations into the store.

Two things changed under conversations that were saved by an earlier version,
and this script handles both in one pass:

  * A recommendation used to come back as Markdown and get scraped apart with
    regexes. It is JSON validated against schema.Recommendation now. That
    scraping is the top half of this file, and this is the only place it still
    runs.
  * Conversations used to be a directory of timestamped JSON files. They are
    rows in SQLite now (see store.py).

    python migrate.py                # import conversations/*.json into the store
    python migrate.py --dry-run      # say what would happen, change nothing
    python migrate.py --dir other/   # import from somewhere else
    python migrate.py --export out/  # write every stored conversation back out

An imported file is renamed to `.json.imported` rather than deleted, so running
this twice does not import anything twice and nothing is thrown away. A
conversation that never gets migrated still opens in the web app; its
recommendation renders as plain Markdown prose rather than as components.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import store
from advisor import extract_mermaid
from schema import Meter, Pillar, Region, Tier
from store import SAVE_VERSION

# --------------------------------------------------------------------------- #
# The old Markdown heuristics, verbatim apart from no longer rendering HTML
# --------------------------------------------------------------------------- #

GOOD_MARKS = ("✅", "✔", "✔️")
REVIEW_MARKS = ("⚠", "⚠️", "❗", "‼")

# Longest first, so stripping "⚠" cannot orphan the variation selector in "⚠️";
# the trailing class clears any selector that survives on its own regardless.
MARK_PATTERN = re.compile(
    "|".join(re.escape(mark) for mark in sorted(GOOD_MARKS + REVIEW_MARKS, key=len, reverse=True))
    + r"|[︎️]"
)

TIER_PATTERN = re.compile(r"\b(low|medium|high)\b", re.IGNORECASE)
HEADING_PATTERN = re.compile(r"^(#{2,4})\s*(.+?)\s*$", re.MULTILINE)
BULLET_PATTERN = re.compile(r"^\s*[-*+]\s+(.*)$")
TABLE_DIVIDER = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")
LABELLED_PILLAR = re.compile(r"^\*\*([^*]+)\*\*\s*[:\-–—]?\s*(.*)$")
PLAIN_PILLAR = re.compile(r"^([A-Za-z ]{3,24})\s*[:\-–—]\s*(.*)$")
RANGE_PATTERN = re.compile(
    r"^\s*(?:\*\*)?\s*(?:to|or|[-–—/])\s*(?:\*\*)?\s*(low|medium|high)\b", re.IGNORECASE
)
COST_LEAD_PATTERN = re.compile(r"^\s*(\*\*)?\s*[:\-–—,.]*\s*")

TRIM_CHARS = " :-–—"

PILLAR_NAMES = tuple(pillar.value for pillar in Pillar)

# What the old parser could produce that the six-pillar enum has no room for.
# Anything else unrecognised is filed under Operations, which is where a note
# about how the thing is run belongs.
# What a service migrated from the old format is charged by: nothing anyone can
# work out from the Markdown it was stored as.
UNPRICED = {
    "meter": Meter.UNPRICED.value,
    "size": "",
    "variant": "",
    "quantity": 0.0,
    "monthly_hours": 0.0,
    # Zero rather than a guess. A reply stored as Markdown said nothing about
    # what any one line costs, and pricing.py reads a zero estimate as "no
    # figure" rather than as free.
    "estimated_monthly_usd": 0.0,
}

PILLAR_ALIASES = {
    "operational excellence": Pillar.OPERATIONS,
    "operations": Pillar.OPERATIONS,
    "cost optimisation": Pillar.COST,
    "cost optimization": Pillar.COST,
    "performance efficiency": Pillar.PERFORMANCE,
}


def _split_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _sections(markdown: str) -> dict[str, str]:
    """Map lower-cased heading text to the body beneath it."""
    found: dict[str, str] = {}
    matches = list(HEADING_PATTERN.finditer(markdown))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        title = match.group(2).strip().lower().rstrip(":")
        found[title] = markdown[match.end() : end].strip()
    return found


def _find(sections: dict[str, str], *keywords: str) -> str:
    """Return the first section whose heading contains all the keywords."""
    for title, body in sections.items():
        if all(word in title for word in keywords):
            return body
    return ""


def _services(body: str) -> list[dict[str, Any]]:
    """Read the Service | Purpose | Reasoning table.

    Not `dict[str, str]`: every row carries a `usage` list as well as the three
    strings, so the narrower annotation this used to have was a lie about the
    shape it returns.
    """
    rows = [
        _split_row(line)
        for line in (raw.strip() for raw in body.split("\n"))
        if line.startswith("|") and not TABLE_DIVIDER.match(line)
    ]
    if len(rows) < 2:
        return []

    header = [cell.lower() for cell in rows[0]]

    def column(name: str, fallback: int) -> int:
        for index, cell in enumerate(header):
            if name in cell:
                return index
        return fallback

    columns = (column("service", 0), column("purpose", 1), column("reason", 2))

    services = []
    for row in rows[1:]:
        size = len(row)
        name, purpose, reasoning = (row[at].strip() if at < size else "" for at in columns)
        if name:
            services.append(
                {
                    "name": name,
                    "purpose": purpose,
                    "reasoning": reasoning,
                    # An old reply is prose about services, with no instance
                    # types, no quantities and no region behind it. There is
                    # nothing here to price, and saying so is the honest answer;
                    # inventing a size would put a number on the screen that
                    # nobody ever recommended.
                    "usage": [UNPRICED],
                }
            )
    return services


def _pillar(label: str) -> str:
    """Fit whatever the old parser called a pillar into the six the schema has."""
    lowered = label.strip().lower()
    if lowered in PILLAR_ALIASES:
        return PILLAR_ALIASES[lowered].value
    for name in PILLAR_NAMES:
        if name.lower() in lowered:
            return name
    return Pillar.OPERATIONS.value


def _notes(body: str) -> list[dict[str, str]]:
    """Read the Well-Architected bullets into pillar / status / text."""
    notes = []
    for line in body.split("\n"):
        bullet = BULLET_PATTERN.match(line)
        if not bullet:
            continue
        text = bullet.group(1).strip()

        # A tick wins outright; only an unticked warning counts as needing review.
        review = any(mark in text for mark in REVIEW_MARKS) and not any(
            mark in text for mark in GOOD_MARKS
        )
        text = MARK_PATTERN.sub("", text).strip(TRIM_CHARS)

        pillar = ""
        # "**Reliability**: text", "Reliability - text", or a bare pillar name.
        labelled = LABELLED_PILLAR.match(text)
        if labelled:
            pillar, text = labelled.group(1).strip(), labelled.group(2).strip()
        else:
            plain = PLAIN_PILLAR.match(text)
            if plain:
                label = plain.group(1).lower()
                if any(p.lower() in label for p in PILLAR_NAMES):
                    pillar, text = plain.group(1).strip(), plain.group(2).strip()

        if not pillar:
            lowered = text.lower()
            for candidate in PILLAR_NAMES:
                if candidate.lower() in lowered:
                    pillar = candidate
                    break

        if not text:
            continue
        notes.append(
            {
                "pillar": _pillar(pillar.strip(TRIM_CHARS)),
                "status": "review" if review else "good",
                "text": text,
            }
        )
    return notes


TIER_BY_NAME = {
    ("low", None): Tier.LOW,
    ("medium", None): Tier.MEDIUM,
    ("high", None): Tier.HIGH,
    ("low", "medium"): Tier.LOW_MEDIUM,
    ("medium", "high"): Tier.MEDIUM_HIGH,
}


def _cost(body: str) -> dict[str, str] | None:
    """Read the cost tier and the range that follows it.

    Handles a straddled tier such as "**Low–Medium** — £150-400/month", which
    would otherwise report Low and leave "Medium" stranded at the front of the
    detail line. A tier the old parser could not name at all is recorded as
    Medium with the original wording kept in the detail, so nothing is invented
    and nothing is lost.

    The detail these carry is in pounds, because the product was in pounds when
    they were written, and it stays that way. Restating an old range in dollars
    would mean inventing an exchange rate to relabel a figure nobody checked.
    The monthly figure a migrated conversation is shown comes from pricing.py in
    dollars regardless, so the two do not sit side by side as rival headlines.
    """
    if not body.strip():
        return None

    flat = " ".join(body.split())
    match = TIER_PATTERN.search(flat)
    if not match:
        return {"tier": Tier.MEDIUM.value, "detail": f"(tier not stated) {flat.replace('**', '')}"}

    first = match.group(1).lower()
    detail = flat[match.end() :]

    second = None
    ranged = RANGE_PATTERN.match(detail)
    if ranged:
        second = ranged.group(1).lower()
        detail = detail[ranged.end() :]

    tier = TIER_BY_NAME.get((first, second)) or TIER_BY_NAME[(first, None)]
    detail = COST_LEAD_PATTERN.sub("", detail).replace("**", "").strip()
    return {"tier": tier.value, "detail": detail}


# --------------------------------------------------------------------------- #
# Migration
# --------------------------------------------------------------------------- #


def as_recommendation(text: str) -> dict[str, Any] | None:
    """Read one old Markdown reply as a recommendation, or None if it is prose."""
    diagram, prose = extract_mermaid(text)
    sections = _sections(prose)

    services = _services(_find(sections, "service"))
    notes = _notes(_find(sections, "architected") or _find(sections, "notes"))
    cost = _cost(_find(sections, "cost"))
    if not (services or notes or cost):
        return None

    headline = " ".join(_find(sections, "headline").split()).replace("**", "").strip().rstrip(".")
    return {
        "headline": headline,
        "overview": _find(sections, "overview"),
        # Old replies never named a region. London is where this tool's users
        # are, and nothing is priced off it anyway: every service comes across
        # as unpriced.
        "region": Region.LONDON.value,
        "services": services,
        "notes": notes,
        "cost": cost or {"tier": Tier.MEDIUM.value, "detail": "(no cost tier recorded)"},
        "diagram": diagram or "",
    }


IMPORTED_SUFFIX = ".json.imported"


def convert(payload: dict[str, Any]) -> int:
    """Turn every old-format reply in a conversation into JSON, in place.

    Returns how many were converted. A follow-up answer is prose under both
    schemes and is left exactly as it is.
    """
    converted = 0
    for message in payload.get("messages") or []:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        recommendation = as_recommendation(str(message.get("content") or ""))
        if recommendation is None:
            continue
        message["content"] = json.dumps(recommendation, indent=2, ensure_ascii=False)
        converted += 1

    if converted or int(payload.get("version") or 1) < SAVE_VERSION:
        payload["version"] = SAVE_VERSION
    return converted


def import_file(path: Path, dry_run: bool = False) -> str:
    """Read one saved file into the store. Returns a line describing what happened."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return f"skipped  {path.name}  ({e})"

    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        return f"skipped  {path.name}  (not a saved conversation)"

    converted = convert(payload)
    detail = f", {converted} repl{'y' if converted == 1 else 'ies'} converted" if converted else ""
    if dry_run:
        return f"would import  {path.name}{detail}"

    try:
        conversation_id = store.import_json(payload)
    except Exception as e:  # a bad file should not stop the rest of the directory
        return f"skipped  {path.name}  ({e})"

    path.rename(path.with_suffix(IMPORTED_SUFFIX))
    return f"imported  {path.name}  -> conversation {conversation_id}{detail}"


def import_directory(directory: Path, dry_run: bool = False) -> list[str]:
    """Import every saved conversation in a directory, oldest first."""
    files = sorted(directory.glob("conversation_*.json"))
    return [import_file(path, dry_run) for path in files]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python migrate.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dir", default="conversations", help="where the saved files are")
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    parser.add_argument(
        "--export",
        metavar="DIR",
        help="write every stored conversation out as JSON instead of importing",
    )
    parser.add_argument(
        "--reindex",
        action="store_true",
        help="rebuild the region, cost tier and services every conversation is findable by",
    )
    args = parser.parse_args(argv)

    if args.reindex:
        # For conversations saved before those columns existed (F8). Reads the
        # replies that are already stored, so it costs nothing and can be run
        # again whenever a doubt arises.
        rebuilt = store.reindex()
        plural = "" if rebuilt == 1 else "s"
        print(f"Reindexed {rebuilt} conversation{plural}.")
        return 0

    if args.export:
        written = store.export_all(Path(args.export))
        plural = "" if len(written) == 1 else "s"
        print(f"Wrote {len(written)} conversation{plural} to {args.export}.")
        return 0

    directory = Path(args.dir)
    if not directory.is_dir():
        print(f"No such directory: {directory}")
        return 1

    lines = import_directory(directory, args.dry_run)
    if not lines:
        print(f"Nothing to import from {directory}.")
        return 0

    for line in lines:
        print(line)
    if not args.dry_run:
        print(f"\nImported files kept alongside as *{IMPORTED_SUFFIX} in {directory}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
