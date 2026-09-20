"""Turning a reviewed architecture into files a client can keep (F3).

The app already holds everything a handover needs: the recommendation, the
diagram, the services, the Well-Architected notes and the priced estimate. Until
now that lived in a browser tab or a JSON document only the app could read. This
module writes it out as a bundle:

    report.md      CommonMark with pipe tables, for Confluence, Notion or a repo
    report.html    one self-contained page, fonts and logo inlined, opens offline
    report.pdf     A4, branded cover, running header and footer
    terraform/     a Terraform module for the architecture (F4), from terraform.py

One document model renders the three reports, so a client reading the PDF and an
engineer reading the Markdown are looking at the same section 3. Nothing is
re-derived per format: the numbers are formatted once, here.

The Terraform module is the exception, and the only thing in the bundle that is
not a rendering of the document. It comes from terraform.py, off the same
`Recommendation` the report is built from, and this module decides only where it
lands in the folder and what travels with it.

Two rules the formats share. A section with no data behind it is left out rather
than printed with "None", so an architecture nothing could be costed for simply
has no estimate table. And every figure says where it came from.

There is one monthly figure and it is in US dollars (F12). It is the AWS Price
List wherever there is a list price to find, and the advisor's own estimate for
the lines there is not -- CloudFront, Route 53, a NAT gateway -- and every line
says which of the two it is. The band beside it is derived from that total rather
than claimed separately, so the band and the figure cannot contradict each other
the way the sterling tier and the dollar estimate used to. No exchange rate is
invented here, the same rule advisor.py follows for token costs.

The PDF is printed by headless Chrome, which is already what tests/browser.py
drives. That keeps the PDF and the HTML the same document rather than two
implementations that drift, and adds no Python dependency. Where there is no
Chrome, the PDF is refused with a sentence saying so; report.html carries the
same @page rules, so printing it from a browser gives the same pages.
"""

from __future__ import annotations

import base64
import html
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import branding
import terraform as tf
from parse import md_to_html, parse_diagram
from pricing import TIER_GAP_WORTH_SAYING
from schema import PILLAR_DOCS, PILLAR_OFFICIAL, Compliance, Pillar, Recommendation, Status

log = logging.getLogger("advisor.export")

ROOT = Path(__file__).parent
ASSETS = ROOT / "static" / "assets"
FONTS = ASSETS / "fonts"

# The palette, from the one place it is written down. A deliverable has to open
# with no stylesheet, so every colour below is written into the file as a
# literal -- but they are the same literals the browser is served as
# /brand.css, so the PDF and the app cannot disagree. The keys are the CSS
# custom property names with the `--cs-` dropped.
#
# The alpha tints further down (rgba(31,157,85,...) and rgba(217,119,6,...))
# stay hard-coded: they derive from the semantic colours, which branding.py
# deliberately does not expose as a customisation point.
INK = branding.palette()

FONT_FAMILY = "'IBM Plex Sans Condensed', system-ui, -apple-system, 'Segoe UI', sans-serif"
MONO_FAMILY = "'IBM Plex Mono', ui-monospace, SFMono-Regular, Menlo, monospace"

# Which font files go into a self-contained page. The latin subsets only: the
# latin-ext files cover accents no report has used, and every one of them is
# 16 KB of base64 in a file that has to travel by email. Sterling, the em dash
# and the middot are all in the latin subset.
INLINE_FONTS = (
    ("IBM Plex Sans Condensed", 300, "ibm-plex-sans-condensed-300-latin.woff2"),
    ("IBM Plex Sans Condensed", 600, "ibm-plex-sans-condensed-600-latin.woff2"),
    ("IBM Plex Sans Condensed", 700, "ibm-plex-sans-condensed-700-latin.woff2"),
    ("IBM Plex Mono", 400, "ibm-plex-mono-400-latin.woff2"),
)

# The logo files and the registered address all come from branding.py, which is
# the one file to edit to rebrand the application.
LOGO = branding.LOGO_FULL
MARK = branding.LOGO_MARK
LOGO_TYPE = "image/svg+xml"

# The registered address. It goes in the footer of anything that leaves the
# building.
COMPANY = branding.BRAND_ADDRESS
# What a compliance slug is called in a document a client reads (F5). The slugs
# are schema.Compliance; the sentences the model is given are in advisor.py, and
# these are neither -- a cover page wants the name of the regime, not an
# instruction about it.
COMPLIANCE_NAMES: dict[str, str] = {
    Compliance.UK_DATA_RESIDENCY: "UK data residency",
    Compliance.PCI_DSS: "PCI DSS",
    Compliance.HIPAA: "HIPAA",
    Compliance.NHS_DSPT: "NHS DSPT",
}

CONFIDENCE = (
    "Commercial in confidence. This architecture is advisory and should be "
    "reviewed by a named engineer before it is built."
)

MAX_META = 120


class ExportError(Exception):
    """A deliverable could not be written. Carries a sentence for the user."""

    def __init__(self, message: str, kind: str = "export") -> None:
        super().__init__(message)
        self.kind = kind


# --------------------------------------------------------------------------- #
# The document model
# --------------------------------------------------------------------------- #


def _clean(text: Any, limit: int = MAX_META) -> str:
    """One line of user-supplied metadata: collapsed, trimmed and capped."""
    return " ".join(str(text or "").split())[:limit].strip()


@dataclass(frozen=True)
class Meta:
    """Who the document is for, and what goes in the appendices.

    `reference` is the document's own identifier, printed on the cover and in
    every footer so a page torn out of a bundle can still be traced back.
    """

    client: str = ""
    prepared_by: str = ""
    reference: str = ""
    prepared_on: date = field(default_factory=date.today)
    transcript: bool = True
    diagram_source: bool = False
    # The regime the review was built to satisfy, as a slug from
    # schema.Compliance, or "" for a review that named none (F5). Printed rather
    # than interpreted: this document does not claim compliance, it records what
    # the architecture was asked to stand up to.
    compliance: str = ""

    @classmethod
    def from_request(cls, payload: dict[str, Any], today: date | None = None) -> Meta:
        """Read the export dialog's fields off a request body."""
        on = today or date.today()
        return cls(
            client=_clean(payload.get("client")),
            prepared_by=_clean(payload.get("preparedBy")),
            reference=_clean(payload.get("reference"), 40) or reference_for(on),
            prepared_on=on,
            transcript=bool(payload.get("transcript", True)),
            diagram_source=bool(payload.get("diagramSource", False)),
            compliance=_clean(payload.get("compliance"), 40),
        )

    @property
    def client_label(self) -> str:
        return self.client or "the client"

    @property
    def long_date(self) -> str:
        """19 August 2026. British order, no leading zero on the day."""
        return f"{self.prepared_on.day} {self.prepared_on:%B %Y}"

    @property
    def iso_date(self) -> str:
        return self.prepared_on.isoformat()


@dataclass(frozen=True)
class Option:
    """One reviewed architecture, as it appears in a deliverable.

    A single review has one of these. A comparison has two, and `label` and
    `recommended` are what let the document say which is which.
    """

    recommendation: Recommendation
    estimate: dict[str, Any] | None = None
    brief: str = ""
    label: str = ""
    recommended: bool = False
    # What region the review stated. None means "whatever the recommendation
    # says", which is every current one. An empty string is a review from before
    # the field existed: those are still worth exporting, and a document must say
    # nothing rather than name a region nobody chose.
    stated_region: str | None = None

    @property
    def title(self) -> str:
        """What to call this architecture.

        The headline is the model's own summary. A conversation brought forward
        from before that field existed has none, so the brief that produced it
        stands in, and the label ("Option B") is the last resort.
        """
        headline = self.recommendation.headline.strip()
        if headline:
            return headline
        brief = " ".join(self.brief.split())
        if not brief:
            return self.label
        return brief[:60].rstrip(" .,") + ("…" if len(brief) > 60 else "")

    @property
    def region(self) -> str:
        """Where this runs, or '' if the review did not say."""
        if self.stated_region is not None:
            return self.stated_region
        return self.recommendation.region.value

    @property
    def priced(self) -> bool:
        """Whether any of this came from the AWS Price List."""
        return bool(self.estimate and self.estimate.get("priced"))

    @property
    def has_figure(self) -> bool:
        """Whether there is a monthly figure at all, wherever it came from.

        This is what a section gates on. `priced` is the narrower question of
        provenance, and answers what the prose around the figure should say.
        """
        return bool(self.estimate and self.estimate.get("hasFigure"))

    @property
    def estimated(self) -> bool:
        """Whether part of the figure is the advisor's own estimate."""
        return bool(self.estimate and self.estimate.get("anyEstimated"))

    @property
    def monthly(self) -> float:
        return float((self.estimate or {}).get("monthlyUsd") or 0.0)

    @property
    def estimated_monthly(self) -> float:
        return float((self.estimate or {}).get("estimatedUsd") or 0.0)

    @property
    def priced_monthly(self) -> float:
        return float((self.estimate or {}).get("pricedUsd") or 0.0)

    @property
    def band(self) -> str:
        """The cost band the document leads with.

        Derived from the figure wherever there is one, which is the whole point
        of F12. The model's own tier is the fallback only when there is no figure
        at all -- an export runs off an estimate the browser cached, and that can
        be absent or have failed. Where it fires, the prose says it is judgement.
        """
        if self.has_figure:
            return str((self.estimate or {}).get("tier") or "")
        return self.recommendation.cost.tier.value

    @property
    def tier_gap(self) -> int:
        """How far the model's own band sits from the arithmetic, in half-bands."""
        return int((self.estimate or {}).get("tierGap") or 0)


@dataclass(frozen=True)
class Deliverable:
    """Everything one export writes out, in the order it is written."""

    options: list[Option]
    meta: Meta
    transcript: list[tuple[str, str]] = field(default_factory=list)
    model: str = ""

    @property
    def compare(self) -> bool:
        return len(self.options) > 1

    @property
    def first(self) -> Option:
        return self.options[0]

    @property
    def title(self) -> str:
        """What the document is called. A comparison is named after neither option."""
        if self.compare:
            return "Architecture options review"
        return self.first.recommendation.headline

    @property
    def subject(self) -> str:
        """What the document is about, in one line.

        The headline is the model's own summary of the architecture and is the
        right title. Conversations migrated from before that field existed have
        none, so the brief the client themselves wrote stands in: a document with
        an empty heading is worse than one titled in the client's own words.
        """
        return self.first.title or self.kind

    @property
    def kind(self) -> str:
        return "Architecture options review" if self.compare else "Architecture review"

    @property
    def brief(self) -> str:
        """The workload as the user stated it, which is what a client recognises."""
        return self.first.brief

    @property
    def preferred(self) -> Option | None:
        """The option the advisor would build, if it named one."""
        return next((option for option in self.options if option.recommended), None)


# --------------------------------------------------------------------------- #
# Naming
# --------------------------------------------------------------------------- #

SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slug(text: str, fallback: str = "review") -> str:
    """A filename-safe fragment of a name: lower case, hyphens, nothing else."""
    cleaned = SLUG_STRIP.sub("-", str(text or "").lower()).strip("-")
    return cleaned[:60].strip("-") or fallback


def reference_for(on: date, sequence: int = 1) -> str:
    """A document reference in the brand's own format: AR-2026-0819-01.

    The prefix comes from branding.DOC_PREFIX.
    """
    return f"{branding.DOC_PREFIX}-{on:%Y-%m%d}-{sequence:02d}"


def basename(deliverable: Deliverable) -> str:
    """What every file in the bundle is called, without its extension.

    architecture-review-northbridge-mutual-2026-08-19, or architecture-options
    for a comparison. Named after the client rather than the workload, because a
    consultant looking in Downloads a week later is looking for the client.
    """
    stem = "architecture-options" if deliverable.compare else "architecture-review"
    parts = [stem]
    if deliverable.meta.client:
        parts.append(slug(deliverable.meta.client))
    elif not deliverable.compare:
        parts.append(slug(deliverable.subject, "advisor"))
    parts.append(deliverable.meta.iso_date)
    return "-".join(parts)


# --------------------------------------------------------------------------- #
# Money and figures
# --------------------------------------------------------------------------- #


def usd(amount: float) -> str:
    """A monthly figure, with a thousands separator and two decimals."""
    return f"${amount:,.2f}"


def usd_round(amount: float) -> str:
    """The same figure where the pennies would be noise, as in a headline."""
    return f"${amount:,.0f}"


# --------------------------------------------------------------------------- #
# The diagram, as SVG
#
# A port of renderDiagram() in static/app.js, so an exported diagram is the one
# the app draws rather than a second interpretation of the same Mermaid. The
# geometry constants and the node palette below are that function's, and
# tests/test_export.py asserts they still match it.
#
# The one thing that cannot be ported is measuring: the browser asks a canvas how
# wide a label renders, and there is no canvas here. Widths are estimated from
# the font's own proportions and rounded up, so a box is occasionally a little
# roomier than the app's and never too small for its label.
# --------------------------------------------------------------------------- #

NODE_HEIGHT = 42
NODE_GAP_Y = 16
COLUMN_GAP = 54
NODE_PAD_X = 34
NODE_MIN_W = 92
NODE_FONT_SIZE = 14
EDGE_LABEL_SIZE = 11

# Boundaries: a VPC, and availability zones inside it (F7). The same values as
# app.js, which is what tests/test_export.py checks.
MAX_GROUP_LEVELS = 2
GROUP_PAD = 12
GROUP_HEAD = 20
GROUP_RADIUS = 10
MAX_BANDS = 6

# The same six the app draws with: static/app.js builds them from the tokens it
# is served, and this imports them, so an exported diagram is the one that was
# on screen.
NODE_STYLES: dict[str, dict[str, Any]] = branding.node_styles()

STANDBY_RE = re.compile(r"replica|standby|secondary|failover|backup|archive")
CACHE_RE = re.compile(r"cache|redis|memcach|elasticache")
DATA_RE = re.compile(
    r"rds|aurora|dynamo|timestream|redshift|documentdb|neptune|database|postgres|mysql|sql"
)
COMPUTE_RE = re.compile(
    r"ec2|lambda|fargate|\becs\b|\beks\b|auto scaling|\basg\b|batch|app runner|compute"
)

# Advance widths as a fraction of the font size, for IBM Plex Sans Condensed at
# weight 600. Enough classes to keep a long label from overflowing its box.
_NARROW = set(r"ijltfI'`.,:;!|()[]/\-·")
_MEDIUM = set("rsczxJ")
_WIDE = set("mwMW@")


def _text_width(label: str, size: int = NODE_FONT_SIZE) -> float:
    """Roughly how wide a node label renders, erring wide rather than narrow."""
    total = 0.0
    for character in label:
        if character == " ":
            total += 0.26
        elif character in _NARROW:
            total += 0.30
        elif character in _MEDIUM:
            total += 0.45
        elif character in _WIDE:
            total += 0.80
        elif character.isupper() or character.isdigit():
            total += 0.56
        else:
            total += 0.50
    return total * size


def node_style(node: dict[str, Any]) -> dict[str, Any]:
    """Pick a node's treatment from what the service is. Mirrors app.js."""
    label = str(node.get("label") or "").lower()
    if STANDBY_RE.search(label):
        return NODE_STYLES["standby"]
    if node.get("entry"):
        return NODE_STYLES["entry"]
    if CACHE_RE.search(label):
        return NODE_STYLES["cache"]
    if DATA_RE.search(label):
        return NODE_STYLES["data"]
    if COMPUTE_RE.search(label):
        return NODE_STYLES["compute"]
    return NODE_STYLES["plain"]


def group_pad(group: Mapping[str, Any]) -> float:
    """How much room a boundary needs around its members: more for an outer one."""
    return GROUP_PAD * (MAX_GROUP_LEVELS - int(group.get("level") or 0))


def group_dash(level: int) -> str:
    """Two dash patterns rather than two colours, so the palette gains nothing."""
    return "4 4" if level == 0 else "2 4"


def _assign_bands(
    used: list[list[dict[str, Any]]],
    groups: Sequence[Mapping[str, Any]],
    group_of: Mapping[str, str],
) -> tuple[dict[str, int], int] | None:
    """Rows, allocated once for the whole graph. A port of assignBands in app.js.

    The reason this exists rather than each column centring its own nodes is in
    that function's comment: it is what makes a box drawn round a group of nodes
    a one-dimensional problem in each axis instead of a packing problem, and it is
    what makes containment a consequence of the ordering rather than a hope.

    None where the graph would need more bands than are readable.
    """
    owners: list[str] = []

    def walk(parent: str | None) -> None:
        for group in groups:
            if group.get("parent") == parent:
                owners.append(str(group["id"]))
                walk(str(group["id"]))

    walk(None)
    owners.append("")

    def owner_of(node: Mapping[str, Any]) -> str:
        return group_of.get(str(node["id"]), "")

    rows = dict.fromkeys(owners, 0)
    for column in used:
        counts: dict[str, int] = {}
        for node in column:
            key = owner_of(node)
            counts[key] = counts.get(key, 0) + 1
        for key, count in counts.items():
            if count > rows.get(key, 0):
                rows[key] = count

    start: dict[str, int] = {}
    total = 0
    for key in owners:
        start[key] = total
        total += rows.get(key, 0)
    if total > MAX_BANDS:
        return None

    band_of: dict[str, int] = {}
    for column in used:
        at: dict[str, int] = {}
        for node in column:
            key = owner_of(node)
            index = at.get(key, 0)
            at[key] = index + 1
            band_of[str(node["id"])] = start[key] + index
    return band_of, total


def _group_nodes(group: Mapping[str, Any], groups: Sequence[Mapping[str, Any]]) -> list[str]:
    """Every node inside a boundary, its own and its descendants'."""
    inside = [str(member) for member in group.get("members") or []]
    for other in groups:
        if other.get("parent") == group.get("id"):
            inside.extend(_group_nodes(other, groups))
    return inside


def _label_width(text: str) -> float:
    """How wide an edge label renders. Mirrors labelWidth in app.js."""
    return _text_width(text, EDGE_LABEL_SIZE)


def _fit_label(label: str, room: float) -> str:
    """An edge label, cut to the gap it is drawn in. Mirrors fitLabel in app.js.

    A backstop rather than the mechanism: the gutter is widened to fit the labels
    crossing it, so this only bites where a label could not be given its own gap --
    an arrow that skips a column, whose midpoint is over a node rather than in a
    gutter.
    """
    if _label_width(label) <= room - 8:
        return label
    cut = label
    while len(cut) > 1 and _label_width(f"{cut}…") > room - 8:
        cut = cut[:-1]
    return f"{cut}…" if len(cut) > 1 else ""


def _group_label(label: str, box_width: float) -> str:
    """A boundary's name, cut to what its box can hold."""
    room = box_width - 20
    if _text_width(label, EDGE_LABEL_SIZE) <= room:
        return label
    cut = label
    while len(cut) > 1 and _text_width(f"{cut}…", EDGE_LABEL_SIZE) > room:
        cut = cut[:-1]
    return f"{cut}…"


def layout_diagram(source: str) -> dict[str, Any] | None:
    """Where everything goes. A port of layoutDiagram in app.js.

    None where there is nothing to draw. The one thing that cannot be ported is
    measuring, which is why the widths here are estimated from the font's own
    proportions; see the note at the top of this section.
    """
    diagram = parse_diagram(str(source or ""))
    nodes = (diagram or {}).get("nodes") or []
    if not nodes:
        return None

    columns: dict[int, list[dict[str, Any]]] = {}
    for node in nodes:
        columns.setdefault(int(node.get("depth") or 0), []).append(node)
    used = [columns[depth] for depth in sorted(columns)]

    groups: list[Mapping[str, Any]] = list((diagram or {}).get("groups") or [])
    group_of: dict[str, str] = {}
    for group in groups:
        for member in group.get("members") or []:
            group_of[str(member)] = str(group["id"])

    # No boundaries, or too many bands for the result to be readable: the centred
    # layout this engine has always drawn, unchanged to the pixel.
    banding = _assign_bands(used, groups, group_of) if groups else None
    if banding is None:
        groups = []
    band_of = banding[0] if banding else {}
    band_count = banding[1] if banding else 0

    column_of: dict[str, int] = {}
    for index, column in enumerate(used):
        for node in column:
            column_of[str(node["id"])] = index
    inside = {str(group["id"]): _group_nodes(group, groups) for group in groups}

    # Room for the labels, reserved before anything is placed. A gutter holds no
    # nodes, which is why a label goes in one -- but being in the gutter is not
    # enough, it has to fit, and a gutter is only COLUMN_GAP wide until something
    # asks for more. A named arrow asks, in the gap in front of the node it points
    # at, which is where its label is drawn.
    gap_need = [0.0] * len(used)
    for edge in (diagram or {}).get("edges") or []:
        label = str(edge.get("label") or "")
        target = column_of.get(str(edge.get("to")))
        if not label or not target:
            continue
        gap_need[target - 1] = max(gap_need[target - 1], _label_width(label) + 8)

    def gap_after(index: int) -> float:
        return max(COLUMN_GAP, gap_need[index])

    # Room for the boundaries, reserved before anything is placed: a box is drawn
    # round its members, so the space it needs has to already be between them.
    left_pad = [0.0] * len(used)
    right_pad = [0.0] * len(used)
    above: dict[int, float] = {}
    below: dict[int, float] = {}

    for group in groups:
        members = inside[str(group["id"])]
        cols = [column_of[member] for member in members if member in column_of]
        bands = [band_of[member] for member in members if member in band_of]
        if not cols or not bands:
            continue
        pad = group_pad(group)
        left_pad[min(cols)] += pad
        right_pad[max(cols)] += pad
        above[min(bands)] = above.get(min(bands), 0.0) + pad + GROUP_HEAD
        below[max(bands)] = below.get(max(bands), 0.0) + pad

    layout: dict[str, dict[str, float]] = {}
    # Where each column ended up, so a label can be centred in the gap in front of
    # it however much padding a boundary put there.
    col_x: list[float] = []
    col_w: list[float] = []

    def column_width(column: list[dict[str, Any]]) -> float:
        return max(
            NODE_MIN_W,
            *(_text_width(str(node.get("label") or "")) + NODE_PAD_X for node in column),
        )

    if banding:
        band_y: list[float] = []
        y = 0.0
        for band in range(band_count):
            y += above.get(band, 0.0)
            band_y.append(y)
            y += NODE_HEIGHT + below.get(band, 0.0)
            if band < band_count - 1:
                y += NODE_GAP_Y
        height = y

        x = 0.0
        for index, column in enumerate(used):
            x += left_pad[index]
            width = column_width(column)
            col_x.append(x)
            col_w.append(width)
            for node in column:
                layout[str(node["id"])] = {
                    "x": x,
                    "y": band_y[band_of[str(node["id"])]],
                    "w": width,
                    "h": NODE_HEIGHT,
                }
            x += width + right_pad[index]
            if index < len(used) - 1:
                x += gap_after(index)
        total_width = x
    else:
        tallest = max(len(column) for column in used)
        height = tallest * NODE_HEIGHT + (tallest - 1) * NODE_GAP_Y

        x = 0.0
        for index, column in enumerate(used):
            width = column_width(column)
            column_height = len(column) * NODE_HEIGHT + (len(column) - 1) * NODE_GAP_Y
            y = (height - column_height) / 2
            col_x.append(x)
            col_w.append(width)
            for node in column:
                layout[str(node["id"])] = {"x": x, "y": y, "w": width, "h": NODE_HEIGHT}
                y += NODE_HEIGHT + NODE_GAP_Y
            x += width + (gap_after(index) if index < len(used) - 1 else 0.0)
        total_width = x

    # The boxes: the union of what is in them, and nothing fixed anywhere.
    # Innermost first, so an outer box can take its children's boxes into its own
    # union and is guaranteed to enclose them.
    drawn: dict[str, dict[str, Any]] = {}
    for group in sorted(groups, key=lambda item: -int(item.get("level") or 0)):
        parts = [layout[member] for member in group.get("members") or [] if member in layout]
        parts += [
            drawn[str(child["id"])]
            for child in groups
            if child.get("parent") == group.get("id") and str(child["id"]) in drawn
        ]
        if not parts:
            continue
        pad = group_pad(group)
        left = min(box["x"] for box in parts) - pad
        right = max(box["x"] + box["w"] for box in parts) + pad
        top = min(box["y"] for box in parts) - pad - GROUP_HEAD
        bottom = max(box["y"] + box["h"] for box in parts) + pad
        drawn[str(group["id"])] = {
            "x": left,
            "y": top,
            "w": right - left,
            "h": bottom - top,
            "label": str(group.get("label") or ""),
            "level": int(group.get("level") or 0),
        }

    # Defence against a bug rather than the thing keeping this honest: a box that
    # has caught a node it does not own is not drawn. A diagram missing a boundary
    # is a smaller lie than one drawn round the wrong thing.
    boundaries: list[dict[str, Any]] = []
    for group in groups:
        box = drawn.get(str(group["id"]))
        if not box:
            continue
        own = set(inside[str(group["id"])])
        swallowed = False
        for node in nodes:
            if str(node["id"]) in own:
                continue
            at = layout.get(str(node["id"]))
            if (
                at
                and at["x"] < box["x"] + box["w"]
                and at["x"] + at["w"] > box["x"]
                and at["y"] < box["y"] + box["h"]
                and at["y"] + at["h"] > box["y"]
            ):
                swallowed = True
                break
        if not swallowed:
            boundaries.append(box)

    return {
        "width": int(total_width) + (1 if total_width % 1 else 0),
        "height": int(height) + (1 if height % 1 else 0),
        "layout": layout,
        "nodes": nodes,
        "edges": (diagram or {}).get("edges") or [],
        "groups": boundaries,
        "columnOf": column_of,
        "colX": col_x,
        "colW": col_w,
    }


def diagram_svg(source: str, standalone: bool = False) -> str:
    """Lay a Mermaid flowchart out as SVG, or return '' if there is nothing to draw.

    `standalone` adds the XML namespace and a white ground, for the .svg file in
    the bundle. Inline in a report the surrounding card supplies the ground.
    """
    plan = layout_diagram(source)
    if not plan:
        return ""

    layout = plan["layout"]

    # First, so the nodes and the arrows paint over the boundary rather than
    # under it. Outermost first among themselves, for the same reason.
    boundaries = []
    for box in plan["groups"]:
        label = html.escape(_group_label(str(box["label"]), box["w"]))
        boundaries.append(
            f'<g><rect x="{box["x"]:g}" y="{box["y"]:g}" width="{box["w"]:g}" '
            f'height="{box["h"]:g}" rx="{GROUP_RADIUS}" fill="none" '
            f'stroke="{INK["line-strong"]}" stroke-width="1" '
            f'stroke-dasharray="{group_dash(int(box["level"]))}"/>'
            f'<text x="{box["x"] + 10:g}" y="{box["y"] + 13:g}" '
            f'font-family="{html.escape(FONT_FAMILY, quote=True)}" '
            f'font-size="{EDGE_LABEL_SIZE}" font-weight="600" letter-spacing="0.04em" '
            f'fill="{INK["secondary"]}">{label}</text></g>'
        )

    # Where a label has already been drawn. Two edges crossing the same gutter on
    # the same row would write their labels on top of each other, so the first one
    # there keeps the slot and the rest become the arrow's tooltip: a label that
    # cannot be read is worse than one that is only in the source.
    taken: set[tuple[int, int]] = set()
    edges = []
    for edge in plan["edges"]:
        start = layout.get(str(edge.get("from")))
        end = layout.get(str(edge.get("to")))
        if not start or not end or start is end:
            continue
        x1 = start["x"] + start["w"]
        y1 = start["y"] + start["h"] / 2
        x2 = end["x"]
        y2 = end["y"] + end["h"] / 2
        middle = x1 + (x2 - x1) / 2
        # Straight where the rows line up, otherwise step through the gutter.
        straight = abs(y1 - y2) < 1
        if straight:
            path = f"M{x1:g} {y1:g} H{x2 - 7:g}"
        else:
            path = f"M{x1:g} {y1:g} H{middle:g} V{y2:g} H{x2 - 7:g}"

        # One rule for both path shapes: centred in the gutter in front of the node
        # the arrow points at, just above the height it arrives at. That gap was
        # widened to fit it, it holds no nodes, and using the target's row rather
        # than the source's is what keeps a fan of arrows out of one another's way --
        # five edges leaving one node arrive at five different rows, so their
        # labels stack instead of landing on the same spot.
        whole = str(edge.get("label") or "")
        target = plan["columnOf"].get(str(edge.get("to")))
        room = end["x"] - (plan["colX"][target - 1] + plan["colW"][target - 1]) if target else 0.0
        label = _fit_label(whole, room) if whole and room > 24 else ""
        drawn_label = ""
        title = f"<title>{html.escape(whole)}</title>" if whole and not label else ""
        if label:
            at_x = end["x"] - room / 2
            at_y = y2 - 6
            slot = (round(at_x / 8), round(at_y / 8))
            if slot in taken:
                title = f"<title>{html.escape(whole)}</title>"
            else:
                taken.add(slot)
                # Haloed in white: the riser of a stepped arrow passes through
                # where the label sits, and paint-order puts the stroke behind the
                # glyphs so the line is broken by the words rather than drawn over
                # them.
                drawn_label = (
                    f'<text x="{at_x:g}" y="{at_y:g}" text-anchor="middle" '
                    'dominant-baseline="auto" '
                    f'font-family="{html.escape(FONT_FAMILY, quote=True)}" '
                    f'font-size="{EDGE_LABEL_SIZE}" font-weight="400" '
                    f'fill="{INK["secondary"]}" stroke="{INK["surface"]}" stroke-width="3" '
                    'stroke-linejoin="round" paint-order="stroke"'
                    f">{html.escape(label)}</text>"
                )
        edges.append(
            f'<path d="{path}" fill="none" stroke="{INK["line-strong"]}" '
            f'stroke-width="1.5" marker-end="url(#cs-arrow)">{title}</path>{drawn_label}'
        )

    boxes = []
    for node in plan["nodes"]:
        box = layout[str(node["id"])]
        style = node_style(node)
        dash = ' stroke-dasharray="5 4"' if style.get("dashed") else ""
        label = html.escape(str(node.get("label") or ""))
        boxes.append(
            f'<g><rect x="{box["x"]:g}" y="{box["y"]:g}" width="{box["w"]:g}" '
            f'height="{box["h"]:g}" rx="8" fill="{style["fill"]}" '
            f'stroke="{style["stroke"]}" stroke-width="1"{dash}/>'
            f'<text x="{box["x"] + box["w"] / 2:g}" y="{box["y"] + box["h"] / 2:g}" '
            'text-anchor="middle" dominant-baseline="central" '
            f'font-family="{html.escape(FONT_FAMILY, quote=True)}" '
            f'font-size="{NODE_FONT_SIZE}" font-weight="600" '
            f'fill="{style["text"]}">{label}</text></g>'
        )

    width_px = plan["width"]
    height_px = plan["height"]
    namespace = ' xmlns="http://www.w3.org/2000/svg"' if standalone else ""
    ground = (
        f'<rect width="{width_px}" height="{height_px}" fill="{INK["surface"]}"/>'
        if standalone
        else ""
    )
    return (
        f'<svg{namespace} width="{width_px}" height="{height_px}" '
        f'viewBox="0 0 {width_px} {height_px}" role="img" '
        'aria-label="Architecture diagram"><defs>'
        '<marker id="cs-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" '
        'markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M0 0 L8 4 L0 8 z" fill="{INK["line-strong"]}"/></marker></defs>'
        f"{ground}{''.join(boundaries)}{''.join(edges)}{''.join(boxes)}</svg>"
    )


# --------------------------------------------------------------------------- #
# Markdown
#
# CommonMark with pipe tables, which is the intersection of what Confluence,
# Notion, GitHub and a plain text editor all read. Front matter is kept short for
# the same reason: an importer that does not understand it shows it as a table
# rather than choking, and a long one looks like a mistake.
# --------------------------------------------------------------------------- #

# What the architecture was sized on (F10), as opposed to ASSUMPTION below,
# which is the caveat on the cost table. The two are different things that share
# a word: this one is the model's stated premises, that one says the quantities
# under the figures were never measured.
ASSUMPTIONS_LEAD = (
    "The architecture was sized on the following, taken as read because the brief did "
    "not say. Each one is a figure worth checking: if one is wrong, the sizing that "
    "rests on it is wrong with it."
)

ASSUMPTION = (
    "**Assumption, not a measurement.** The quantities behind these lines are the "
    "model's own sizing of the architecture, not measured usage. Re-price against "
    "real traffic before the figure goes into a business case."
)

UNPRICED_NOTE = (
    "The AWS Price List could not be read for this architecture and the advisor put no "
    "figure on it either, so there is no priced estimate. The band above is the "
    "advisor's own judgement, unchecked against any published price."
)


def estimated_note(option: Option) -> str:
    """Why part of a total is judgement rather than arithmetic.

    Named services rather than a count, because "3 services are estimated" tells
    a reader nothing they can act on, and "CloudFront and Route 53" tells them
    exactly which figures to challenge.
    """
    names = [
        str(service.get("name") or "")
        for service in (option.estimate or {}).get("services") or []
        if service.get("estimated")
    ]
    named = ", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else "".join(names)
    return (
        f"**{usd(option.estimated_monthly)} of this total is the advisor's own estimate** "
        f"rather than a published price. {named or 'Some of these services'} "
        f"{'is' if len(names) == 1 else 'are'} not in the price list files this reads, so "
        "those lines carry the advisor's figure and are marked in the table. Challenge "
        "them first."
    )


def _cell(text: Any) -> str:
    """One table cell: no newlines, and pipes escaped so the row survives."""
    return " ".join(str(text or "").split()).replace("|", "\\|")


def _table(header: list[str], rows: list[list[str]], align: str = "") -> str:
    """A pipe table. `align` is one character per column: l, c or r."""
    marks = []
    for index in range(len(header)):
        side = align[index] if index < len(align) else "l"
        marks.append({"r": "----:", "c": ":---:"}.get(side, "----"))
    return "\n".join(
        [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(marks) + " |",
            *("| " + " | ".join(row) + " |" for row in rows),
        ]
    )


def _spoken(role: str, text: str) -> str:
    """One turn of the transcript, in words rather than in what was stored.

    An assistant turn that gave an architecture is stored as the JSON the schema
    validated. That is the right thing to store and the wrong thing to print: a
    client reading Appendix B would find several thousand characters of one-line
    JSON where a sentence belongs, and a conversation that revised the
    architecture would carry two of them.

    The architecture is in section 2, where it belongs. Here the turn is recorded
    as having happened, with the headline it was given, so the transcript still
    reads as the conversation it was.
    """
    plain = " ".join(str(text or "").split())
    if role == "user" or not plain.startswith("{"):
        return plain

    try:
        data = json.loads(plain)
    except ValueError:
        return plain
    if not isinstance(data, dict) or "headline" not in data or "services" not in data:
        return plain

    headline = " ".join(str(data.get("headline") or "").split()).rstrip(".")
    services = [
        service
        for service in data.get("services") or []
        if isinstance(service, dict) and str(service.get("name") or "").strip()
    ]
    count = f"{len(services)} service{'' if len(services) == 1 else 's'}"
    where = str(data.get("region") or "").strip()
    shape = f"{count} in {where}" if where else count
    if headline:
        return f"Recommended an architecture: {headline} ({shape})."
    return f"Recommended an architecture of {shape}."


def _quote(text: str) -> str:
    """A block quote. Wrapping is left to whatever renders it."""
    lines = [line.strip() for line in str(text or "").strip().split("\n")]
    return "\n".join(f"> {line}" if line else ">" for line in lines)


def _basis(service: dict[str, Any]) -> str:
    """What one service was priced on, as one cell of a table."""
    return "; ".join(str(line.get("detail") or "") for line in service.get("lines") or [])


def _estimate_services(option: Option) -> list[dict[str, Any]]:
    """An estimate's services, dearest first, the ones with no figure last.

    Sorted on the figure rather than on where it came from: a $500 estimate
    matters more to the reader than a $6 list price, and burying it below the
    priced lines would say otherwise.
    """
    return sorted(
        (option.estimate or {}).get("services") or [],
        key=lambda service: (
            not (service.get("priced") or service.get("estimated")),
            -float(service.get("monthlyUsd") or 0.0),
        ),
    )


def _figure(service: Mapping[str, Any], marker: str = " *(est.)*") -> str:
    """One service's monthly figure, saying so where it is an estimate."""
    amount = usd(float(service.get("monthlyUsd") or 0.0))
    if service.get("priced"):
        return amount
    if service.get("estimated"):
        return f"{amount}{marker}"
    return "*no figure*"


def _estimate_rows(option: Option) -> list[list[str]]:
    """One Markdown row per service in an estimate."""
    return [
        [f"**{_cell(service.get('name'))}**", _cell(_basis(service)) or "—", _figure(service)]
        for service in _estimate_services(option)
    ]


def coverage(option: Option) -> tuple[int, list[str]]:
    """How many of the six pillars this review spoke to, and which it did not (F6).

    The same arithmetic parse.py does for the screen, against the same enum, so
    the document and the app cannot show a reader two different numbers.
    """
    named = {note.pillar for note in option.recommendation.notes}
    missing = [PILLAR_OFFICIAL[pillar] for pillar in Pillar if pillar not in named]
    return len(Pillar) - len(missing), missing


def _md_notes(option: Option) -> str:
    """The Well-Architected notes, one paragraph a pillar, gaps marked.

    Each pillar is a link to the framework's own page for it (F6). The name the
    reader sees is AWS's, not the app's shorthand: a client checking a review
    against the framework should not have to work out that "Cost" meant Cost
    Optimization.
    """
    parts = []
    for note in option.recommendation.notes:
        flag = "⚠ *Needs review.* " if note.status is Status.REVIEW else ""
        name = PILLAR_OFFICIAL[note.pillar]
        link = f"[{name}]({PILLAR_DOCS[note.pillar]})"
        parts.append(f"**{link}** — {flag}{note.text.strip()}")

    covered, missing = coverage(option)
    parts.append(
        f"*{covered} of {len(Pillar)} pillars are covered above."
        + (f" Not addressed: {', '.join(missing)}.*" if missing else "*")
    )
    return "\n\n".join(parts)


def diagram_stem(option: Option, deliverable: Deliverable) -> str:
    """What this option's diagram files are called in the bundle."""
    if not deliverable.compare:
        return "diagram"
    return f"diagram-{slug(option.label, 'option')}"


def _md_option_body(option: Option, deliverable: Deliverable, level: str = "##") -> list[str]:
    """The per-option sections, at whatever heading depth the document needs.

    Every block starts with its own heading, so the caller can number them: a
    single review numbers 1 to 5, a comparison nests them under each option.
    """
    recommendation = option.recommendation
    blocks: list[str] = []

    if recommendation.overview.strip():
        blocks.append(f"{level} Recommendation\n\n{recommendation.overview.strip()}")

    if recommendation.diagram.strip():
        where = f" in {option.region}" if option.region else ""
        alt = f"Reference architecture, {len(recommendation.services)} services{where}"
        stem = diagram_stem(option, deliverable)
        blocks.append(
            f"{level} Reference architecture\n\n"
            f"![{alt}]({diagram_image(option, deliverable)})\n\n"
            f"*Vector source also written to `{stem}.svg`.*"
        )

    if recommendation.services:
        rows = [
            [f"**{_cell(service.name)}**", _cell(service.purpose), _cell(service.reasoning)]
            for service in recommendation.services
        ]
        blocks.append(
            f"{level} Services\n\n" + _table(["Service", "Purpose", "Why this one"], rows)
        )

    if recommendation.notes:
        blocks.append(f"{level} Well-Architected notes\n\n{_md_notes(option)}")

    if recommendation.assumptions:
        assumed = "\n".join(f"- {_cell(line)}" for line in recommendation.assumptions)
        blocks.append(f"{level} What the sizing rests on\n\n{ASSUMPTIONS_LEAD}\n\n{assumed}")

    estimate = option.estimate or {}
    if option.has_figure:
        cost = [
            f"{level} Cost",
            "",
            f"**{option.band} — {usd_round(option.monthly)} / month**",
            "",
            f"{'Part list price, part estimate' if option.estimated else 'On-demand'} "
            f"AWS Price List rates, {estimate.get('region')}, retrieved "
            f"{str(estimate.get('pricedAt') or '')[:10]}. Excludes support plan, tax and any "
            "existing commitments. In US dollars, as AWS publishes them. The band is "
            "derived from the figure above, not claimed separately.",
            "",
            _table(
                ["Line", "Basis", "USD / month"],
                [*_estimate_rows(option), ["**Total**", "", f"**{usd(option.monthly)}**"]],
                align="llr",
            ),
            "",
            f"⚠ {ASSUMPTION}",
        ]
        if option.estimated:
            cost += ["", estimated_note(option)]
        if option.tier_gap >= TIER_GAP_WORTH_SAYING:
            cost += [
                "",
                f"⚠ The advisor's own read of this architecture was "
                f"**{recommendation.cost.tier.value}**, and the figures put it at "
                f"**{option.band}**. A full band apart means something has been "
                "mis-sized; check the quantities before quoting either.",
            ]
        cost += [
            "",
            f"The advisor's own read, written before anything was looked up: "
            f"{recommendation.cost.detail.strip()}",
        ]
    else:
        cost = [
            f"{level} Cost",
            "",
            f"**{option.band}** — {recommendation.cost.detail.strip()}",
            "",
            UNPRICED_NOTE,
        ]
    blocks.append("\n".join(cost))
    return blocks


def _labels(deliverable: Deliverable) -> list[str]:
    """Each option's heading, with the recommended one marked once."""
    labels = []
    for index, option in enumerate(deliverable.options, start=1):
        label = option.label or f"Option {index}"
        labels.append(f"{label} · recommended" if option.recommended else label)
    return labels


def _shared_estimate(options: Sequence[Option]) -> list[list[str]]:
    """The same rows _twin_lines builds, formatted as Markdown cells.

    This used to compute them itself, character for character the same as
    _twin_lines below -- the same dedup, the same `priced or estimated` gate, the
    same sort key -- and differ only in what it returned. Two copies of the
    arithmetic behind a client's cost comparison is the one thing this module's
    own docstring says it does not do: the numbers are worked out once.
    """

    def cell(entry: Entry | None) -> str:
        if entry is None:
            return "—"
        amount, estimated = entry
        return f"{usd(amount)} *(est.)*" if estimated else usd(amount)

    rows = [
        [f"**{_cell(name)}**", *(cell(entry) for entry in entries)]
        for name, entries in _twin_lines(options)
    ]
    rows.append(["**Total per month**", *(f"**{usd(option.monthly)}**" for option in options)])
    return rows


def verdict(deliverable: Deliverable) -> str:
    """What the document says about which option to build.

    The advisor is not asked to choose between two workloads it reviewed
    separately, so unless something marked one as preferred this says exactly
    that. Implying a recommendation nobody made is the one thing a comparison
    document must not do.
    """
    options = deliverable.options
    preferred = deliverable.preferred
    if preferred is not None:
        return (
            f"We would build **{preferred.label or preferred.title}**. "
            f"{preferred.recommendation.overview.strip()}"
        )

    count = "Both options" if len(options) == 2 else f"All {len(options)} options"
    lines = [
        f"{count} were reviewed against the same brief and none is marked as "
        "preferred: which one to build is a judgement about your constraints rather "
        "than one this review makes for you."
    ]
    costed = [option for option in options if option.has_figure]
    if len(costed) > 1:
        cheapest = min(costed, key=lambda option: option.monthly)
        dearest = max(costed, key=lambda option: option.monthly)
        gap = dearest.monthly - cheapest.monthly
        if gap > 1:
            basis = (
                "On list prices and estimates"
                if any(option.estimated for option in costed)
                else "On list prices"
            )
            lines.append(
                f"{basis}, {cheapest.label or 'the first option'} is the least "
                f"expensive, at {usd(gap)} a month less than "
                f"{dearest.label or 'the dearest'} at the sizes given."
            )
    return "\n\n".join(lines)


def _md_compare(deliverable: Deliverable) -> list[str]:
    """The comparison's own sections, before each option's detail.

    Side-by-side prose does not survive a plain-text reader, so a paired section
    becomes a row in an attribute table instead of two columns of paragraphs.
    """
    options = deliverable.options
    heads = _labels(deliverable)

    def monthly(option: Option) -> str:
        """One option's figure and band in one cell.

        One row, not two. A cost tier row beside a priced-per-month row is the
        same architecture costed twice in a table, which is F12's complaint in
        miniature.
        """
        if not option.has_figure:
            return f"*not costed* — {_cell(option.recommendation.cost.detail)}"
        marker = " (part estimate)" if option.estimated else ""
        return f"**{usd(option.monthly)}**{marker} — {option.band}"

    rows = [
        ["**Approach**", *(_cell(option.title) for option in options)],
        ["**Region**", *(option.region or "—" for option in options)],
        ["**Monthly**", *(monthly(option) for option in options)],
        ["**Services**", *(str(len(option.recommendation.services)) for option in options)],
    ]
    blocks = ["## Side by side\n\n" + _table(["", *heads], rows)]

    if all(option.has_figure for option in options):
        estimated = any(option.estimated for option in options)
        basis = (
            "Every column uses the same AWS Price List, so a service more than one "
            "option uses costs the same in each. Lines marked *(est.)* have no list "
            "price and carry the advisor's own figure."
            if estimated
            else "Every column is priced from the same AWS Price List, so a service "
            "more than one option uses costs the same in each."
        )
        blocks.append(
            "## Cost, line by line\n\n"
            f"{basis}\n\n"
            + _table(
                ["Line", *heads],
                _shared_estimate(options),
                align="l" + "r" * len(options),
            )
            + f"\n\n⚠ {ASSUMPTION}"
        )

    blocks.append(f"## Recommendation\n\n{verdict(deliverable)}")
    return _numbered(blocks)


def _numbered(blocks: list[str]) -> list[str]:
    """Number a single review's sections 1 to n, in the order they were built."""
    numbered = []
    for number, block in enumerate(blocks, start=1):
        head, newline, body = block.partition("\n")
        numbered.append(head.replace("## ", f"## {number}. ", 1) + newline + body)
    return numbered


def _md_appendices(deliverable: Deliverable) -> list[str]:
    """The brief, the transcript and the diagram source, where they are wanted."""
    meta = deliverable.meta
    parts = []

    if deliverable.brief.strip():
        about = []
        if deliverable.model:
            about.append(f"reviewed by {deliverable.model}")
        exchanges = sum(1 for role, _ in deliverable.transcript if role == "user")
        if exchanges:
            about.append(f"{exchanges} exchange{'' if exchanges == 1 else 's'}")
        note = f"Submitted {meta.iso_date}" + (f" · {' · '.join(about)}." if about else ".")
        parts.append(
            f"## Appendix A — The brief, as stated\n\n{_quote(deliverable.brief)}\n\n{note}"
        )

    if meta.transcript and len(deliverable.transcript) > 1:
        turns = [
            f"**{'Client' if role == 'user' else 'Advisor'}** — {_spoken(role, text)}"
            for role, text in deliverable.transcript
        ]
        parts.append("## Appendix B — Full transcript\n\n" + "\n\n".join(turns))

    if meta.diagram_source:
        blocks = []
        for index, option in enumerate(deliverable.options, start=1):
            source = option.recommendation.diagram.strip()
            if not source:
                continue
            named = f"{option.label or f'Option {index}'}\n\n" if deliverable.compare else ""
            blocks.append(f"{named}```mermaid\n{source}\n```")
        if blocks:
            parts.append("## Appendix C — Diagram source\n\n" + "\n\n".join(blocks))

    return parts


def markdown(deliverable: Deliverable) -> str:
    """The whole report as one Markdown document."""
    meta = deliverable.meta
    first = deliverable.first

    front = ["---", f"title: {deliverable.kind} — {deliverable.subject}"]
    if meta.client:
        front.append(f"client: {meta.client}")
    if meta.prepared_by:
        front.append(f"prepared_by: {meta.prepared_by}")
    front += [f"date: {meta.iso_date}", f"reference: {meta.reference}"]
    if first.region:
        front.append(f"region: {first.region}")
    # `cost_band`, not `cost_tier`: the value is derived from the figure below it
    # now, and a machine reader that kept quoting a field called "tier" would be
    # quoting the model's judgement long after the product stopped showing it.
    front.append(f"cost_band: {first.band}")
    if first.has_figure:
        front.append(f"monthly_usd: {first.monthly:.2f}")
        if first.estimated:
            front.append(f"monthly_usd_estimated: {first.estimated_monthly:.2f}")
    if first.tier_gap >= TIER_GAP_WORTH_SAYING:
        front.append(f"cost_band_claimed: {first.recommendation.cost.tier.value}")
    front += ["confidence: Advisory — review before build", "---"]

    prepared = []
    if meta.client:
        prepared.append(f"**Prepared for** {meta.client}")
    if meta.prepared_by:
        prepared.append(f"**Prepared by** {meta.prepared_by}")

    parts = [
        "\n".join(front),
        f"# {deliverable.kind} — {deliverable.subject}",
        (" · ".join(prepared) + "  \n" if prepared else "")
        + f"**Date** {meta.long_date} · **Reference** {meta.reference}",
        _quote(CONFIDENCE),
        "---",
    ]

    if deliverable.compare:
        parts += _md_compare(deliverable)
        for label, option in zip(_labels(deliverable), deliverable.options, strict=True):
            parts.append(f"## {label} — {option.title}" if option.title else f"## {label}")
            parts += _md_option_body(option, deliverable, level="###")
    else:
        parts += _numbered(_md_option_body(first, deliverable))

    parts.append("---")
    parts += _md_appendices(deliverable)

    generated = [f"Generated by the {branding.BRAND_NAME} {branding.PRODUCT_NAME}"]
    if deliverable.model:
        generated.append(deliverable.model)
    generated.append(datetime.now().astimezone().isoformat(timespec="seconds"))
    generated.append("Commercial in confidence")
    generated.append(COMPANY)
    parts.append("*" + " · ".join(generated) + "*")

    return "\n\n".join(part for part in parts if part).strip() + "\n"


# --------------------------------------------------------------------------- #
# HTML: the sections both pages are built from
#
# One set of builders, two shells. report.html wraps them in a hero, a set of
# section links and a footer; the print sheet wraps each one in an A4 page with
# its own header and footer. Doing it the other way round -- a stylesheet per
# format over separate markup -- is how the two drift apart.
# --------------------------------------------------------------------------- #


def esc(text: Any) -> str:
    """Escape for HTML text. Everything user- or model-supplied goes through this."""
    return html.escape(str(text or ""), quote=True)


def _inline_md(text: str) -> str:
    """A field's own Markdown, rendered by the app's renderer.

    parse.md_to_html is the same subset the app draws on screen, and its link
    check is what keeps a `javascript:` URL out of a file someone will open from
    an email (S1).
    """
    return md_to_html(str(text or "").strip())


ONE_PARAGRAPH = re.compile(r"^<p>(?!.*<p>)(.*)</p>$", re.DOTALL)


def _phrase(text: str) -> str:
    """The same, unwrapped where the field is a single sentence.

    A note reads "Needs review. The design is single-region", on one line. Left
    as a paragraph, the badge and the flag sit above the sentence they belong to.
    """
    rendered = _inline_md(text)
    found = ONE_PARAGRAPH.match(rendered)
    return found.group(1) if found else rendered


@dataclass(frozen=True)
class Section:
    """One numbered part of the report, as both shells need it."""

    key: str
    title: str
    body: str
    number: str = ""

    @property
    def eyebrow(self) -> str:
        return f"{self.number} · {self.key}" if self.number else self.key

    @property
    def anchor(self) -> str:
        return slug(self.key, "section")


def _diagram_card(option: Option) -> str:
    """The architecture, drawn. Returns '' when there is nothing to draw."""
    svg = diagram_svg(option.recommendation.diagram)
    if not svg:
        return ""
    return (
        '<div class="card card--diagram"><div class="diagram">'
        f"{svg}</div></div>"
        '<p class="caption">Vector, at print resolution. Arrows follow the request path.</p>'
    )


def _services_table(option: Option) -> str:
    """The services table: what each one does, and why that one."""
    rows = "".join(
        "<tr>"
        f'<td><span class="service">{esc(service.name)}</span>'
        f'<span class="service__why">{_phrase(service.reasoning)}</span></td>'
        f"<td>{_phrase(service.purpose)}</td>"
        "</tr>"
        for service in option.recommendation.services
    )
    return (
        '<table class="table"><thead><tr>'
        "<th>Service &amp; why</th><th>Purpose</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
    )


def _notes_list(option: Option) -> str:
    """The Well-Architected notes, with the gaps marked as gaps.

    The pillar badge is an anchor to the framework's page for it (F6). It is the
    one outbound link in the document: everything else, fonts and diagram and
    logo included, is inlined so the page opens with no network at all. An
    anchor fetches nothing until somebody clicks it, so that property holds.
    """
    items = []
    for note in option.recommendation.notes:
        review = note.status is Status.REVIEW
        name = PILLAR_OFFICIAL[note.pillar]
        badge = f'<a class="pillar" href="{esc(PILLAR_DOCS[note.pillar])}">{esc(name)}</a>'
        items.append(
            f'<div class="note{" note--review" if review else ""}">'
            f"{badge}"
            f'<span class="note__text">{_phrase(note.text)}</span></div>'
        )

    covered, missing = coverage(option)
    caption = f"{covered} of {len(Pillar)} pillars covered." + (
        f" Not addressed: {esc(', '.join(missing))}." if missing else ""
    )
    return f'<div class="notes">{"".join(items)}</div><p class="caption">{caption}</p>'


def _assumptions_list(option: Option) -> str:
    """What the sizing rests on, stated rather than buried in the prose (F10)."""
    items = "".join(
        f'<li class="assumed__item">{_phrase(line)}</li>'
        for line in option.recommendation.assumptions
    )
    return f'<p class="assumed__lead">{esc(ASSUMPTIONS_LEAD)}</p><ul class="assumed">{items}</ul>'


def _cost_band(option: Option, accent: bool = True) -> str:
    """The band and the figure, in the panel the design gives them.

    Only a comparison uses this: it holds the two columns' rows aligned and it
    is where the accent marks the option the advisor would build. A single review
    puts the same figure in the head of _estimate_table() instead, because two
    panels saying the same thing is the bug F12 exists to fix.

    The accent marks one thing only, so where a comparison recommended neither
    option, neither column takes it: two accented panels beside each other read as
    two recommendations.
    """
    eyebrow = "eyebrow eyebrow--on-primary" if accent else "eyebrow eyebrow--muted"
    figure = usd_round(option.monthly) if option.has_figure else "Not costed"
    detail = (
        f"{'Part estimate' if option.estimated else 'AWS list prices'}, per month"
        if option.has_figure
        else _phrase(option.recommendation.cost.detail)
    )
    return (
        f'<div class="tier{"" if accent else " tier--plain"}">'
        f'<span class="{eyebrow}">{esc(option.band)}</span>'
        f'<div class="tier__name">{esc(figure)}</div>'
        f'<p class="tier__detail">{detail}</p></div>'
    )


def _estimate_table(option: Option) -> str:
    """The one cost element: the figure, the band, and every line behind it."""
    if not option.has_figure:
        return (
            f'<div class="estimate"><div class="estimate__head">'
            f'<span class="estimate__note">{esc(UNPRICED_NOTE)}</span>'
            f'<span class="estimate__total">{esc(option.band)}</span></div></div>'
        )

    estimate = option.estimate or {}
    rows = []
    for service in _estimate_services(option):
        if service.get("priced"):
            figure, mark = usd(float(service.get("monthlyUsd") or 0.0)), ""
        elif service.get("estimated"):
            figure, mark = (
                f"{usd(float(service.get('monthlyUsd') or 0.0))} est.",
                " line__figure--est",
            )
        else:
            figure, mark = "No figure", " line__figure--none"
        basis = _basis(service)
        rows.append(
            '<div class="line">'
            f'<span class="line__name">{esc(service.get("name"))}</span>'
            f'<span class="line__figure{mark}">{esc(figure)}</span>'
            + (f'<span class="line__basis">{esc(basis)}</span>' if basis else "")
            + "</div>"
        )

    tail = ""
    if option.estimated:
        tail += f'<p class="caption">{_inline_md(estimated_note(option))}</p>'
    if option.tier_gap >= TIER_GAP_WORTH_SAYING:
        tail += (
            f'<p class="caption caption--warn">The advisor\'s own read of this architecture '
            f"was {esc(option.recommendation.cost.tier.value)}, and the figures put it at "
            f"{esc(option.band)}. A full band apart means something has been mis-sized; "
            "check the quantities before quoting either.</p>"
        )

    provenance = (
        f"{estimate.get('linesPriced')} of {estimate.get('linesTotal')} lines from the "
        "AWS Price List, the rest the advisor's own estimate"
        if option.estimated
        else "On-demand AWS Price List rates"
    )
    return (
        '<div class="estimate">'
        '<div class="estimate__head">'
        f'<span class="estimate__note">{esc(provenance)}, '
        f"{esc(estimate.get('region'))}, retrieved "
        f"{esc(str(estimate.get('pricedAt') or '')[:10])}. "
        "Excludes support plan, tax and existing commitments. In US dollars, as AWS "
        f"publishes them. Band: {esc(option.band)}, derived from the figure.</span>"
        f'<span class="estimate__total">{esc(usd_round(option.monthly))}'
        '<span class="estimate__per"> / month</span></span>'
        "</div>"
        f'<p class="assumption"><strong>Assumption, not a measurement.</strong> The quantities '
        "behind these lines are the model's own sizing of the architecture, not measured "
        "usage. Re-price against real traffic before the figure goes into a business case.</p>"
        f'<div class="lines">{"".join(rows)}</div>{tail}</div>'
    )


def sections(deliverable: Deliverable) -> list[Section]:
    """The report's numbered sections, in order, skipping the ones with no data."""
    if deliverable.compare:
        return _compare_sections(deliverable)

    option = deliverable.first
    recommendation = option.recommendation
    built: list[Section] = []

    if recommendation.overview.strip():
        built.append(
            Section(
                "Recommendation",
                "What we recommend, and why",
                _inline_md(recommendation.overview),
            )
        )
    diagram = _diagram_card(option)
    if diagram:
        built.append(Section("Architecture", "Reference architecture", diagram))
    if recommendation.services:
        built.append(Section("Services", "What each service is doing", _services_table(option)))
    if recommendation.notes:
        built.append(Section("Well-Architected", "Notes by pillar", _notes_list(option)))
    if recommendation.assumptions:
        built.append(Section("Assumptions", "What the sizing rests on", _assumptions_list(option)))
    built.append(Section("Cost", "What it costs to run", _estimate_table(option)))

    return [
        Section(section.key, section.title, section.body, f"{index:02d}")
        for index, section in enumerate(built, start=1)
    ]


def _option_column(option: Option, label: str) -> str:
    """One option's header cell in the side-by-side grid."""
    tone = "column--recommended" if option.recommended else ""
    eyebrow = f"{label} · recommended" if option.recommended else label
    return (
        f'<div class="column {tone}">'
        f'<span class="eyebrow">{esc(eyebrow)}</span>'
        f'<div class="column__name">{esc(option.title)}</div></div>'
    )


def _compare_sections(deliverable: Deliverable) -> list[Section]:
    """The comparison's sections: paired rows, one estimate, then the verdict."""
    options = deliverable.options
    labels = _labels(deliverable)
    built: list[Section] = []

    pairs = [
        ("Approach", [_inline_md(option.recommendation.overview) for option in options]),
        ("Region", [f"<p>{esc(option.region)}</p>" if option.region else "" for option in options]),
        ("Monthly", [_cost_band(option, option.recommended) for option in options]),
    ]
    rows = "".join(
        f'<div class="pair"><span class="pair__label">{esc(label)}</span>'
        + "".join(f'<div class="pair__cell">{cell or "<p>—</p>"}</div>' for cell in cells)
        + "</div>"
        for label, cells in pairs
    )
    built.append(
        Section(
            "Side by side",
            "Where the options diverge",
            f'<div class="columns columns--{len(options)}">'
            + "".join(_option_column(option, labels[index]) for index, option in enumerate(options))
            + f"</div>{rows}",
        )
    )

    if all(option.has_figure for option in options):

        def cell(entry: tuple[float, bool] | None) -> str:
            if entry is None:
                return '<span class="twin__figure">—</span>'
            amount, estimated = entry
            mark = " twin__figure--est" if estimated else ""
            suffix = " est." if estimated else ""
            return f'<span class="twin__figure{mark}">{esc(usd(amount) + suffix)}</span>'

        lines = "".join(
            f'<div class="twin twin--{len(options)}">'
            f'<span class="twin__name">{esc(name)}</span>'
            + "".join(cell(entry) for entry in entries)
            + "</div>"
            for name, entries in _twin_lines(options)
        )
        marked = (
            " Lines marked est. have no list price and carry the advisor's own figure."
            if any(option.estimated for option in options)
            else ""
        )
        heads = "".join(f'<span class="twin__figure">{esc(label)}</span>' for label in labels)
        totals = "".join(
            f'<span class="twin__figure">{esc(usd(option.monthly))}</span>' for option in options
        )
        built.append(
            Section(
                "Cost",
                f"One estimate, {len(options)} columns",
                f'<div class="estimate"><div class="twin twin--{len(options)} twin--head">'
                '<span class="twin__name">Line · on demand</span>'
                f"{heads}</div>"
                f"{lines}"
                f'<div class="twin twin--{len(options)} twin--total">'
                '<span class="twin__name">Total per month</span>'
                f"{totals}</div>"
                + (f'<p class="caption">{esc(marked.strip())}</p>' if marked else "")
                + "</div>",
            )
        )

    built.append(
        Section("Recommendation", "Which one we would build", _inline_md(verdict(deliverable)))
    )

    for option, label in zip(deliverable.options, labels, strict=True):
        body = []
        diagram = _diagram_card(option)
        if diagram:
            body.append(diagram)
        if option.recommendation.services:
            body.append(_services_table(option))
        if option.recommendation.notes:
            body.append(_notes_list(option))
        if option.recommendation.assumptions:
            body.append(_assumptions_list(option))
        built.append(Section(label, option.title, "".join(body)))

    return [
        Section(section.key, section.title, section.body, f"{index:02d}")
        for index, section in enumerate(built, start=1)
    ]


Entry = tuple[float, bool]


def _twin_lines(options: Sequence[Option]) -> list[tuple[str, list[Entry | None]]]:
    """Every estimate as one set of rows, dearest first.

    Each cell is (figure, estimated) or None where that option does not use the
    service at all. Estimated lines are carried rather than dropped: a service
    one option needs and the others do not is exactly what a comparison is for.
    """
    width = len(options)
    figures: dict[str, list[Entry | None]] = {}
    for column, option in enumerate(options):
        for service in (option.estimate or {}).get("services") or []:
            name = str(service.get("name") or "")
            figures.setdefault(name, [None] * width)
            if service.get("priced") or service.get("estimated"):
                figures[name][column] = (
                    float(service.get("monthlyUsd") or 0.0),
                    bool(service.get("estimated")),
                )
    return sorted(
        figures.items(),
        key=lambda item: -max(entry[0] if entry else 0.0 for entry in item[1]),
    )


def appendices(deliverable: Deliverable) -> list[Section]:
    """The brief, the transcript and the diagram source, as sections."""
    meta = deliverable.meta
    built: list[Section] = []

    if deliverable.brief.strip():
        about = []
        if deliverable.model:
            about.append(f"reviewed by {esc(deliverable.model)}")
        exchanges = sum(1 for role, _ in deliverable.transcript if role == "user")
        if exchanges:
            about.append(f"{exchanges} exchange{'' if exchanges == 1 else 's'}")
        note = (
            f'<p class="caption">Submitted {esc(meta.iso_date)}'
            + (f" · {' · '.join(about)}" if about else "")
            + "</p>"
        )
        built.append(
            Section(
                "Appendix A",
                "The brief, as stated",
                f'<blockquote class="brief">{esc(deliverable.brief)}</blockquote>{note}',
            )
        )

    if meta.transcript and len(deliverable.transcript) > 1:
        turns = "".join(
            '<div class="turn">'
            f'<span class="turn__who{"" if role == "user" else " turn__who--advisor"}">'
            f"{'Client' if role == 'user' else 'Advisor'}</span>"
            f'<span class="turn__text">{_inline_md(_spoken(role, text))}</span></div>'
            for role, text in deliverable.transcript
        )
        built.append(
            Section(
                "Appendix B",
                "How the recommendation was reached",
                f'<div class="turns">{turns}</div>',
            )
        )

    if meta.diagram_source:
        blocks = []
        for index, option in enumerate(deliverable.options, start=1):
            source = option.recommendation.diagram.strip()
            if not source:
                continue
            name = f"{option.label or f'Option {index}'} · " if deliverable.compare else ""
            blocks.append(
                '<div class="source">'
                f'<div class="source__name">{esc(name)}'
                f"{esc(diagram_stem(option, deliverable))}.mmd</div>"
                f"<pre>{esc(source)}</pre></div>"
            )
        if blocks:
            built.append(Section("Appendix C", "Diagram source", "".join(blocks)))

    return built


# --------------------------------------------------------------------------- #
# Self-contained assets
#
# "Self-contained" is the whole point of report.html: it has to render the same
# with the network switched off, from a Downloads folder, months later. So the
# fonts and the logo travel inside it as data URIs and there is no stylesheet,
# no script and no image request. The files are read once per process, because a
# 74 KB font does not change between two exports.
# --------------------------------------------------------------------------- #

_cache: dict[str, str] = {}


def _data_uri(path: Path, mime: str) -> str:
    """One file as a data URI, or '' if it is not where it should be.

    A missing asset must not fail an export: a report with no logo is still a
    report, and the alternative is a consultant losing a document to a file that
    was never checked in.
    """
    key = str(path)
    if key not in _cache:
        try:
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            _cache[key] = f"data:{mime};base64,{encoded}"
        except OSError:
            _cache[key] = ""
    return _cache[key]


def logo_uri() -> str:
    return _data_uri(ASSETS / LOGO, LOGO_TYPE)


def mark_uri() -> str:
    return _data_uri(ASSETS / MARK, LOGO_TYPE)


def logo_img(css_class: str) -> str:
    """The logo as an `<img>`, or nothing at all when the file cannot be read.

    Every page that carries the logo does so on a dark ground, and all three
    take the company name from branding.py as their alt text.
    """
    uri = logo_uri()
    if not uri:
        return ""
    return f'<img class="{css_class}" src="{uri}" alt="{esc(branding.BRAND_NAME)}">'


def font_css() -> str:
    """@font-face rules with the woff2 files inlined."""
    rules = []
    for family, weight, filename in INLINE_FONTS:
        uri = _data_uri(FONTS / filename, "font/woff2")
        if not uri:
            continue
        rules.append(
            f"@font-face{{font-family:'{family}';font-style:normal;font-weight:{weight};"
            f"font-display:swap;src:url({uri}) format('woff2')}}"
        )
    return "".join(rules)


# --------------------------------------------------------------------------- #
# The stylesheet the two shells share
# --------------------------------------------------------------------------- #

BASE_CSS = f"""
*{{box-sizing:border-box}}
body{{margin:0;font-family:{FONT_FAMILY};font-weight:300;color:{INK["ink"]};
  background:{INK["surface"]};-webkit-font-smoothing:antialiased;
  font-variant-numeric:tabular-nums}}
h1,h2,h3{{font-weight:700;letter-spacing:-0.02em;line-height:1.15;margin:0}}
p{{margin:0 0 12px;line-height:1.7;color:{INK["ink-2"]}}}
p:last-child{{margin-bottom:0}}
strong{{font-weight:600;color:{INK["ink"]}}}
em{{font-style:italic}}
code{{font-family:{MONO_FAMILY};font-size:0.92em}}
a{{color:{INK["accent"]};text-decoration-color:{INK["line-strong"]};text-underline-offset:3px}}
ul,ol{{margin:0 0 12px;padding-left:22px;line-height:1.7;color:{INK["ink-2"]}}}

.eyebrow{{display:block;font-size:12px;font-weight:600;letter-spacing:0.14em;
  text-transform:uppercase;color:{INK["primary"]}}}
.eyebrow--muted{{color:{INK["ink-3"]}}}
.eyebrow--on-primary{{color:rgba(255,255,255,0.8)}}
.caption{{margin:10px 0 0;font-size:14px;line-height:1.55;color:{INK["ink-3"]}}}

.card{{border:1px solid {INK["line"]};border-radius:12px;background:{INK["surface-3"]};
  padding:26px 24px}}
.card--diagram{{overflow-x:auto}}
.diagram svg{{display:block;max-width:100%;height:auto;margin:0 auto}}

.table{{width:100%;border-collapse:separate;border-spacing:0;border:1px solid {INK["line"]};
  border-radius:10px;overflow:hidden}}
.table th{{text-align:left;font-size:12px;font-weight:600;letter-spacing:0.14em;
  text-transform:uppercase;color:{INK["ink-3"]};background:{INK["surface-3"]};
  padding:10px 16px;border-bottom:1px solid {INK["line"]}}}
.table td{{padding:13px 16px;border-bottom:1px solid {INK["line"]};vertical-align:top;
  font-size:15px;line-height:1.5;color:{INK["ink-2"]}}}
.table tr:last-child td{{border-bottom:none}}
.table td p{{margin:0}}
.service{{display:block;font-size:16px;font-weight:600;color:{INK["ink"]}}}
.service__why{{display:block;font-size:15px;color:{INK["ink-3"]};margin-top:2px}}
.service__why p{{margin:0;color:inherit}}

.notes{{display:flex;flex-direction:column;gap:8px}}
.assumed__lead{{margin:0 0 12px;font-size:15px;line-height:1.6;color:{INK["ink-2"]}}}
.assumed{{margin:0;padding:0;list-style:none}}
.assumed__item{{padding:9px 0;border-top:1px solid {INK["line"]};font-size:15px;
line-height:1.55;color:{INK["ink"]}}}
.assumed__item:first-child{{border-top:none}}
.note{{display:flex;gap:10px;padding:12px 14px;border:1px solid {INK["line"]};border-radius:8px}}
.note--review{{border-color:rgba(217,119,6,0.35);background:rgba(217,119,6,0.05)}}
/* An anchor since F6, so the underline and the visited colour have to go: it is
   a badge that happens to be clickable, not a link in a sentence. */
.pillar{{flex:0 0 auto;font-size:11px;font-weight:600;letter-spacing:0.06em;
  text-transform:uppercase;border-radius:999px;padding:2px 8px;height:fit-content;
  color:{INK["success"]};border:1px solid rgba(31,157,85,0.35);
  background:rgba(31,157,85,0.08);text-decoration:none}}
.note--review .pillar{{color:{INK["warning"]};border-color:rgba(217,119,6,0.4);
  background:{INK["surface"]}}}
.note__text{{font-size:15px;line-height:1.55;color:{INK["ink-2"]}}}
.note__text p{{margin:0;color:inherit}}

.tier{{background:linear-gradient(135deg,{INK["primary"]} 0%,{INK["secondary"]} 100%);
  border-radius:12px;padding:24px 28px;margin-bottom:16px}}
.tier__name{{font-size:40px;font-weight:700;line-height:1;color:#fff;margin:8px 0}}
.tier__detail,.tier__detail p{{color:rgba(255,255,255,0.92);font-size:17px;line-height:1.5;
  margin:0}}
/* The same panel with the accent taken out, for an option nothing preferred. */
.tier--plain{{background:{INK["surface-3"]};border:1px solid {INK["line"]}}}
.tier--plain .tier__name{{color:{INK["ink"]}}}
.tier--plain .tier__detail,.tier--plain .tier__detail p{{color:{INK["ink-2"]}}}

.estimate{{border:1px solid {INK["line"]};border-radius:12px;padding:22px 26px;
  background:{INK["surface-3"]}}}
.estimate__head{{display:flex;align-items:baseline;justify-content:space-between;
  gap:16px;flex-wrap:wrap}}
.estimate__note{{flex:1 1 22ch;font-size:14px;line-height:1.5;color:{INK["ink-3"]}}}
.estimate__total{{font-size:32px;font-weight:700;line-height:1;color:{INK["ink"]};
  white-space:nowrap}}
.estimate__per{{font-size:16px;font-weight:300;color:{INK["ink-3"]}}}
.assumption{{margin:12px 0 0;padding:10px 14px;border-left:3px solid {INK["warning"]};
  background:{INK["surface"]};font-size:15px;color:{INK["ink-2"]};line-height:1.5}}
.lines{{margin-top:16px}}
.line{{display:grid;grid-template-columns:1fr auto;gap:0 16px;padding-top:10px;
  border-top:1px solid {INK["line"]};margin-top:10px}}
.line:first-child{{border-top:none;padding-top:0;margin-top:0}}
.line__name{{font-size:16px;font-weight:600;color:{INK["ink"]}}}
.line__figure{{font-size:16px;color:{INK["ink-2"]};white-space:nowrap;text-align:right}}
.line__figure--none{{font-weight:300;color:{INK["ink-3"]}}}
.line__figure--est{{font-style:italic;color:{INK["ink-3"]}}}
.twin__figure--est{{font-style:italic;color:{INK["ink-3"]}}}
.caption--warn{{padding-left:12px;border-left:3px solid {INK["primary"]}}}
.line__basis{{grid-column:1/-1;font-family:{MONO_FAMILY};font-size:11.5px;
  color:{INK["ink-3"]};margin-top:2px}}
.unpriced{{margin:0;padding:12px 16px;border:1px solid {INK["line"]};border-radius:10px;
  background:{INK["surface-3"]};font-size:15px;color:{INK["ink-2"]}}}

/* A comparison is two to four options wide (F9). The page has a fixed width, so
   the columns divide it rather than scrolling the way the app's do. */
.columns{{display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-bottom:16px}}
.columns--3{{grid-template-columns:repeat(3,1fr);gap:16px}}
.columns--4{{grid-template-columns:repeat(4,1fr);gap:12px}}
.columns--3 .column__name,.columns--4 .column__name{{font-size:18px}}
.column{{border-top:3px solid {INK["line-strong"]};padding-top:12px}}
.column--recommended{{border-top-color:{INK["primary"]}}}
.column .eyebrow{{color:{INK["ink-3"]}}}
.column--recommended .eyebrow{{color:{INK["primary"]}}}
.column__name{{font-size:22px;font-weight:700;line-height:1.2;color:{INK["ink"]};margin-top:6px}}
.pair{{display:grid;grid-template-columns:120px 1fr 1fr;gap:24px;padding:16px 0;
  border-top:1px solid {INK["line"]}}}
.columns--3 ~ .pair{{grid-template-columns:110px repeat(3,1fr);gap:16px}}
.columns--4 ~ .pair{{grid-template-columns:100px repeat(4,1fr);gap:12px}}
.pair__label{{font-size:12px;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;
  color:{INK["ink-3"]}}}
.pair__cell{{font-size:16px;line-height:1.65}}

.twin{{display:grid;grid-template-columns:1.5fr 0.75fr 0.75fr;gap:16px;padding:12px 0;
  border-top:1px solid {INK["line"]}}}
.twin--3{{grid-template-columns:1.5fr repeat(3,0.75fr);gap:12px}}
.twin--4{{grid-template-columns:1.4fr repeat(4,0.7fr);gap:10px}}
.twin--4 .twin__name,.twin--4 .twin__figure{{font-size:14px}}
.twin--head{{border-top:none;padding-top:0}}
.twin--head .twin__name,.twin--head .twin__figure{{font-size:12px;font-weight:600;
  letter-spacing:0.14em;text-transform:uppercase;color:{INK["ink-3"]}}}
.twin__name{{font-size:16px;font-weight:600;color:{INK["ink"]}}}
.twin__figure{{font-size:16px;color:{INK["ink-2"]};text-align:right;white-space:nowrap}}
.twin--total .twin__name,.twin--total .twin__figure{{font-size:17px;font-weight:700;
  color:{INK["ink"]}}}

.brief{{margin:0;background:{INK["accent-soft"]};border-radius:10px;padding:20px 24px;
  font-size:17px;line-height:1.7;color:{INK["ink-2"]}}}
.turns{{display:flex;flex-direction:column;gap:14px}}
.turn{{display:flex;gap:12px}}
.turn__who{{flex:0 0 76px;font-family:{MONO_FAMILY};font-size:11px;letter-spacing:0.06em;
  text-transform:uppercase;color:{INK["ink-3"]};padding-top:4px}}
.turn__who--advisor{{color:{INK["primary"]}}}
.turn__text{{flex:1;font-size:16px;line-height:1.6;color:{INK["ink-2"]}}}
.turn__text p{{margin:0 0 8px}}
.source{{border:1px solid {INK["line"]};border-radius:10px;overflow:hidden;margin-bottom:12px}}
.source__name{{padding:10px 16px;background:{INK["surface-3"]};
  border-bottom:1px solid {INK["line"]};font-size:14px;font-weight:600;color:{INK["ink-2"]};
  font-family:{MONO_FAMILY}}}
.source pre{{margin:0;padding:16px;font-family:{MONO_FAMILY};font-size:12.5px;line-height:1.7;
  color:{INK["accent"]};background:{INK["surface"]};white-space:pre-wrap}}
"""

# Nothing in a report may be split across two pages if splitting it makes it
# unreadable: half a diagram, or an estimate whose total landed on the next page.
BREAK_CSS = """
.card,.note,.estimate,.tier,.brief,.source,.turn,.line,.twin,.pair{break-inside:avoid}
.table{break-inside:auto}
.table tr{break-inside:avoid}
h1,h2,h3{break-after:avoid}
"""


# --------------------------------------------------------------------------- #
# report.html: one page, self-contained
# --------------------------------------------------------------------------- #

WEB_CSS = f"""
body{{background:{INK["canvas"]}}}
.page{{max-width:1100px;margin:0 auto;background:{INK["surface"]};
  box-shadow:0 18px 40px rgba(11,30,63,0.14)}}
.hero{{background:{INK["accent"]};padding:44px 56px 0}}
.hero__top{{display:flex;align-items:flex-start;justify-content:space-between;gap:32px}}
.hero__title{{font-size:44px;font-weight:700;line-height:1.08;letter-spacing:-0.025em;
  color:#fff;max-width:24ch;margin:14px 0 0}}
.hero__meta{{margin:18px 0 0;font-size:19px;line-height:1.6;color:rgba(255,255,255,0.78);
  max-width:60ch}}
.hero__logo{{flex:0 0 auto;width:210px;height:auto}}
.stats{{display:flex;gap:36px;flex-wrap:wrap;margin-top:34px}}
.stat__figure{{font-size:30px;font-weight:700;line-height:1.1;color:#fff}}
.stat__figure--accent{{color:{INK["primary-soft"]}}}
.stat__label{{font-size:13px;color:rgba(255,255,255,0.6)}}
.nav{{display:flex;gap:26px;flex-wrap:wrap;margin-top:32px;padding-top:16px;
  border-top:1px solid rgba(255,255,255,0.18)}}
.nav a{{font-size:15px;color:rgba(255,255,255,0.7);padding-bottom:12px;
  text-decoration:none;transition:color 220ms ease}}
.nav a:hover{{color:#fff}}
.body{{padding:48px 56px 56px;display:flex;flex-direction:column;gap:44px}}
.section__title{{font-size:32px;margin:8px 0 16px}}
.foot{{background:{INK["accent"]};padding:32px 56px;display:flex;align-items:flex-end;
  justify-content:space-between;gap:32px}}
.foot__logo{{display:block;width:180px;height:auto;margin-bottom:14px}}
.foot p{{margin:0;font-size:15px;line-height:1.6;color:rgba(255,255,255,0.6);max-width:60ch}}
.foot__site{{font-size:15px;color:rgba(255,255,255,0.6);white-space:nowrap}}
details{{border-top:1px solid {INK["line"]}}}
details:last-of-type{{border-bottom:1px solid {INK["line"]}}}
summary{{display:flex;align-items:center;gap:12px;padding:14px 2px;cursor:pointer;
  font-size:17px;font-weight:600;color:{INK["ink"]};list-style:none}}
summary::-webkit-details-marker{{display:none}}
summary::after{{content:'+';margin-left:auto;font-size:20px;color:{INK["line-strong"]}}}
details[open] summary::after{{content:'\\2212'}}
details > div{{padding:0 2px 20px}}

@media (max-width:900px){{
  .hero{{padding:32px 24px 0}}
  .body{{padding:32px 24px 40px;gap:32px}}
  .foot{{padding:24px;flex-direction:column;align-items:flex-start}}
  .hero__top{{flex-direction:column-reverse;align-items:flex-start}}
  .hero__logo{{width:170px}}
  .hero__title{{font-size:32px}}
  .section__title{{font-size:26px}}
  .columns,.pair{{grid-template-columns:1fr}}
  .pair__label{{margin-bottom:-8px}}
}}

@media print{{
  @page{{size:A4;margin:18mm 16mm}}
  body{{background:#fff}}
  .page{{max-width:none;box-shadow:none}}
  .nav{{display:none}}
  .hero{{padding:0 0 24px}}
  .body{{padding:24px 0;gap:28px}}
  .foot{{padding:20px 0}}
  details{{break-inside:avoid}}
  details > div{{display:block}}
  .section{{break-inside:avoid-page}}
}}
"""


def _stats(deliverable: Deliverable) -> str:
    """The four figures across the top of report.html."""
    option = deliverable.first
    # The money takes the accent, not the band. The band is derived from the
    # figure now, so leading with it would lead with the vaguer restatement of
    # the number sitting next to it.
    figures: list[tuple[str, str, bool]] = [
        (
            usd_round(option.monthly) if option.has_figure else "Not costed",
            (
                ("Estimated per month" if option.estimated else "Priced, per month")
                if option.has_figure
                else "AWS Price List"
            ),
            True,
        ),
        (
            option.band,
            "Cost band" if option.has_figure else "Cost band (advisor's judgement)",
            False,
        ),
        (str(len(option.recommendation.services)), "AWS services", False),
    ]
    if option.region:
        figures.append((option.region, "Region", False))
    covered, _missing = coverage(option)
    figures.append((f"{covered} / {len(Pillar)}", "Pillars covered", False))
    if deliverable.meta.compliance:
        figures.append((COMPLIANCE_NAMES.get(deliverable.meta.compliance, "—"), "Built for", False))
    return "".join(
        f'<div class="stat"><div class="stat__figure'
        f'{" stat__figure--accent" if accent else ""}">{esc(figure)}</div>'
        f'<div class="stat__label">{esc(label)}</div></div>'
        for figure, label, accent in figures
    )


def _prepared_line(deliverable: Deliverable) -> str:
    """Prepared for X by Y on date, with whatever of that was filled in."""
    meta = deliverable.meta
    parts = []
    if meta.client:
        parts.append(f"Prepared for {esc(meta.client)}")
    if meta.prepared_by:
        parts.append(f"by {esc(meta.prepared_by)}")
    parts.append(f"· {esc(meta.long_date)}")
    return " ".join(parts)


def web_html(deliverable: Deliverable) -> str:
    """report.html: the whole review as one self-contained page."""
    meta = deliverable.meta
    body = sections(deliverable)
    extras = appendices(deliverable)

    nav = "".join(f'<a href="#{section.anchor}">{esc(section.key)}</a>' for section in body)
    if extras:
        nav += '<a href="#appendices">Appendices</a>'

    parts = "".join(
        f'<section class="section" id="{section.anchor}">'
        f'<span class="eyebrow">{esc(section.eyebrow)}</span>'
        f'<h2 class="section__title">{esc(section.title)}</h2>'
        f"{section.body}</section>"
        for section in body
    )

    if extras:
        first, *rest = extras
        opened = (
            f'<section class="section" id="appendices">'
            f'<span class="eyebrow">{esc(first.key)}</span>'
            f'<h2 class="section__title">{esc(first.title)}</h2>{first.body}'
        )
        folded = "".join(
            f"<details><summary>{esc(section.key)} · {esc(section.title)}</summary>"
            f"<div>{section.body}</div></details>"
            for section in rest
        )
        parts += f"{opened}{folded}</section>"

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en-GB"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{esc(deliverable.kind)} — {esc(deliverable.subject)}</title>"
        f'<meta name="description" content="{esc(deliverable.kind)} for '
        f'{esc(meta.client_label)}, reference {esc(meta.reference)}.">'
        f"<style>{font_css()}{BASE_CSS}{BREAK_CSS}{WEB_CSS}</style></head><body>"
        '<div class="page">'
        f'<header class="hero"><div class="hero__top"><div>'
        f'<span class="eyebrow">{esc(deliverable.kind)} · {esc(meta.reference)}</span>'
        f'<h1 class="hero__title">{esc(deliverable.subject)}</h1>'
        f'<p class="hero__meta">{_prepared_line(deliverable)}. '
        "Advisory: review with a named engineer before build.</p></div>"
        + logo_img("hero__logo")
        + f'</div><div class="stats">{_stats(deliverable)}</div>'
        f'<nav class="nav">{nav}</nav></header>'
        f'<main class="body">{parts}</main>'
        '<footer class="foot"><div>'
        + logo_img("foot__logo")
        + f"<p>{esc(CONFIDENCE)} {esc(COMPANY)} · {esc(meta.reference)}.</p></div>"
        f'<span class="foot__site">{esc(branding.BRAND_DOMAIN)}</span></footer>'
        "</div></body></html>"
    )


# --------------------------------------------------------------------------- #
# The print sheet, which becomes report.pdf
#
# A4 with an accent-coloured cover that bleeds to the paper edge, which is what the named
# page below is for: Chrome honours `@page cover { margin: 0 }` and gives that
# one page no margins while every other page keeps its 20mm.
#
# The running header and footer are per section rather than fixed. A fixed
# element does repeat on every printed page in Chrome, but it repeats on the
# cover too and cannot be told not to, and positioning one in the page margin is
# unreliable across versions. A section that runs onto a second page therefore
# carries its header on the first page and its footer on the last, which is the
# one deviation from the design mock -- along with "Page 2 of 5", which needs a
# margin box Chrome does not implement.
# --------------------------------------------------------------------------- #

PRINT_CSS = f"""
@page{{size:A4;margin:20mm 20mm 18mm}}
@page cover{{margin:0}}
body{{font-size:15px}}
.cover{{page:cover;height:297mm;background:{INK["accent"]};color:#fff;
  padding:26mm 24mm;display:flex;flex-direction:column;break-after:page}}
.cover__logo{{display:block;width:62mm;height:auto}}
.cover__lead{{margin-top:auto}}
.cover__title{{font-size:46px;font-weight:700;line-height:1.05;letter-spacing:-0.025em;
  color:#fff;max-width:19ch;margin:14px 0 0}}
.cover__blurb{{margin:18px 0 0;font-size:19px;line-height:1.55;
  color:rgba(255,255,255,0.78);max-width:46ch}}
.cover__rule{{height:3px;width:120px;margin:14mm 0 10mm;
  background:linear-gradient(135deg,{INK["primary-soft"]} 0%,{INK["surface"]} 100%)}}
.cover__grid{{display:grid;grid-template-columns:1fr 1fr;gap:8mm 12mm}}
.cover__label{{display:block;font-size:12px;font-weight:600;letter-spacing:0.14em;
  text-transform:uppercase;color:rgba(255,255,255,0.5);margin-bottom:4px}}
.cover__value{{font-size:17px;color:#fff}}
.cover__value--mono{{font-family:{MONO_FAMILY};font-size:16px}}
.cover__foot{{margin-top:12mm;padding-top:6mm;border-top:1px solid rgba(255,255,255,0.18);
  display:flex;justify-content:space-between;gap:20px;font-size:14px;
  color:rgba(255,255,255,0.55)}}

.sheet{{break-before:page}}
.sheet__head{{display:flex;align-items:center;gap:10px;padding-bottom:10px;
  border-bottom:1px solid {INK["line"]};margin-bottom:24px}}
.sheet__mark{{width:22px;height:22px;object-fit:contain}}
.sheet__subject{{font-size:14px;color:{INK["ink-3"]}}}
.sheet__client{{margin-left:auto;font-size:14px;font-weight:600;color:{INK["ink-2"]}}}
.sheet__title{{font-size:30px;margin:8px 0 16px}}
.part{{margin-bottom:26px}}
.part:last-child{{margin-bottom:0}}
.sheet__foot{{margin-top:18px;padding-top:10px;border-top:1px solid {INK["line"]};
  display:flex;justify-content:space-between;gap:20px;font-size:13px;color:{INK["ink-3"]}}}
.tier__name{{font-size:36px}}
.hero,.nav,.foot{{display:none}}
"""


def _cover(deliverable: Deliverable) -> str:
    """The cover: what this is, who it is for, and what it is called."""
    meta = deliverable.meta
    fields = [("Prepared for", meta.client or "—"), ("Prepared by", meta.prepared_by or "—")]
    fields += [("Date", meta.long_date)]
    grid = "".join(
        f'<div><span class="cover__label">{esc(label)}</span>'
        f'<span class="cover__value">{esc(value)}</span></div>'
        for label, value in fields
    )
    grid += (
        '<div><span class="cover__label">Reference</span>'
        f'<span class="cover__value cover__value--mono">{esc(meta.reference)}</span></div>'
    )

    blurb = (
        "A reviewed AWS reference architecture, Well-Architected notes and a monthly "
        f"estimate for {esc(meta.client_label)}."
    )
    if deliverable.compare:
        blurb = (
            "Two reviewed AWS architectures for the same brief, with what each one "
            f"costs to run, for {esc(meta.client_label)}."
        )

    return (
        '<section class="cover">' + logo_img("cover__logo") + '<div class="cover__lead">'
        f'<span class="eyebrow">{esc(deliverable.kind)}</span>'
        f'<h1 class="cover__title">{esc(deliverable.subject)}</h1>'
        f'<p class="cover__blurb">{blurb}</p></div>'
        f'<div class="cover__rule"></div><div class="cover__grid">{grid}</div>'
        f'<div class="cover__foot"><span>{esc(CONFIDENCE)}</span>'
        f"<span>{esc(branding.BRAND_DOMAIN)}</span></div></section>"
    )


# Which sections share a sheet. The design puts the recommendation and the
# diagram on one page, the services and the notes on the next, and the cost on
# its own, rather than starting a page per heading: a short section alone leaves
# most of an A4 sheet empty, and a client counts pages.
#
# A section named nowhere here takes a sheet of its own, in the order it was
# built, so a new one shows up rather than silently joining someone else's page.
PRINT_SHEETS = (
    ("Recommendation", "Architecture"),
    ("Services", "Well-Architected"),
    # The assumptions share the cost's sheet rather than taking one of their own:
    # four sentences alone would leave most of an A4 page empty, and they are the
    # premises the figures beside them rest on (F10).
    ("Assumptions", "Cost"),
    ("Side by side",),
)


def _sheet_groups(built: list[Section]) -> list[list[Section]]:
    """The sections, in print order, gathered onto the sheets they share."""
    groups: list[list[Section]] = []
    started: dict[int, list[Section]] = {}

    for section in built:
        which = next(
            (number for number, keys in enumerate(PRINT_SHEETS) if section.key in keys), None
        )
        if which is None:
            groups.append([section])
            continue
        if which not in started:
            started[which] = []
            groups.append(started[which])
        started[which].append(section)
    return groups


def print_html(deliverable: Deliverable) -> str:
    """The A4 sheet Chrome prints into report.pdf."""
    meta = deliverable.meta
    head = (
        '<header class="sheet__head">'
        + (f'<img class="sheet__mark" src="{mark_uri()}" alt="">' if mark_uri() else "")
        + f'<span class="sheet__subject">{esc(deliverable.kind)} · '
        f"{esc(deliverable.subject)}</span>"
        + (f'<span class="sheet__client">{esc(meta.client)}</span>' if meta.client else "")
        + "</header>"
    )
    foot = (
        f'<footer class="sheet__foot"><span>Commercial in confidence · {esc(COMPANY)}</span>'
        f"<span>{esc(meta.reference)}</span></footer>"
    )

    groups = _sheet_groups(sections(deliverable))
    # Every appendix travels on one flowing sheet. They are the part a reader
    # skims, and a page each would double the length of the document.
    extras = appendices(deliverable)
    if extras:
        groups.append(extras)

    sheets = []
    for group in groups:
        parts = "".join(
            f'<div class="part"><span class="eyebrow">{esc(section.eyebrow)}</span>'
            f'<h2 class="sheet__title">{esc(section.title)}</h2>{section.body}</div>'
            for section in group
        )
        sheets.append(f'<section class="sheet">{head}{parts}{foot}</section>')

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en-GB"><head><meta charset="utf-8">'
        f"<title>{esc(deliverable.kind)} — {esc(deliverable.subject)}</title>"
        f"<style>{font_css()}{BASE_CSS}{BREAK_CSS}{PRINT_CSS}</style></head><body>"
        f"{_cover(deliverable)}{''.join(sheets)}</body></html>"
    )


# --------------------------------------------------------------------------- #
# report.pdf, printed by the browser that is already on the machine
#
# Chrome's --print-to-pdf renders the same print sheet a person would get by
# printing report.html, which is the point: one document, not a second
# implementation in a PDF library. It also means no Python dependency, and
# nothing to install on a machine that already has a browser.
#
# Where there is no Chrome the PDF is refused with a sentence saying so, and the
# rest of the bundle is written anyway. A missing PDF is a smaller problem than
# a failed export.
# --------------------------------------------------------------------------- #

# CHROME_PATH first, so a machine with the browser somewhere unusual can say
# where. The rest mirrors tests/browser.py, which looks in the same places for
# the same reason.
CHROME_CANDIDATES = (
    os.environ.get("CHROME_PATH"),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)

PRINT_FLAGS = (
    "--headless",
    "--disable-gpu",
    # Not --no-sandbox. That flag turns off the browser's main defence while it
    # renders a document built from model output and from strings somebody typed
    # into a dialog, and it was only ever there for the containerised case --
    # Chrome refusing to start as root. --disable-dev-shm-usage covers that case
    # without giving the sandbox up, so the sandbox stays on by default and the
    # insecure path is opt-in and named. See sandbox_flags().
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-background-networking",
    # Every font and image in the document is a data URI, so nothing here should
    # be reaching the network for one.
    "--disable-remote-fonts",
    "--no-pdf-header-footer",
)

# The escape hatch for a deployment that genuinely cannot run the sandbox -- a
# container without the right capabilities, most likely. Set
# ADVISOR_CHROME_NO_SANDBOX=1 and it comes back, so the choice is somebody's
# rather than the default nobody noticed.
NO_SANDBOX_ENV = "ADVISOR_CHROME_NO_SANDBOX"


def sandbox_flags() -> tuple[str, ...]:
    """`--no-sandbox`, only where it has been asked for by name."""
    if os.environ.get(NO_SANDBOX_ENV) == "1":
        log.warning("%s=1: printing with the Chrome sandbox disabled", NO_SANDBOX_ENV)
        return ("--no-sandbox",)
    return ()


# Long enough for a cold browser start on a busy laptop, short enough that a
# hung print does not hold a request open until the browser gives up.
PRINT_TIMEOUT = 90.0

NO_CHROME = (
    "No browser was found to print the PDF with. Chrome or Edge makes report.pdf; "
    "without one, open report.html and print it from your own browser, which "
    "produces the same pages. Set CHROME_PATH if it is installed somewhere unusual."
)


def chrome() -> str | None:
    """The browser this machine can print with, or None."""
    for candidate in CHROME_CANDIDATES:
        if candidate and Path(candidate).is_file():
            return candidate
    return (
        shutil.which("google-chrome")
        or shutil.which("chromium")
        or shutil.which("chrome")
        or shutil.which("msedge")
    )


def pdf(sheet: str, binary: str | None = None) -> bytes:
    """Print the sheet and hand back the PDF.

    Raises ExportError, never a subprocess error: every failure here is a
    sentence the dialog can show.
    """
    binary = binary or chrome()
    if not binary:
        raise ExportError(NO_CHROME, kind="no_browser")

    with tempfile.TemporaryDirectory(prefix="advisor-pdf-") as work:
        source = Path(work) / "report.html"
        out = Path(work) / "report.pdf"
        # utf-8 explicitly: the report is full of pound signs and em dashes, and
        # a Windows default of cp1252 would refuse to write them.
        source.write_text(sheet, encoding="utf-8")

        try:
            finished = subprocess.run(
                [
                    binary,
                    *PRINT_FLAGS,
                    *sandbox_flags(),
                    # Its own profile, so printing never touches the browser the
                    # person is using and never waits on a locked one.
                    f"--user-data-dir={Path(work) / 'profile'}",
                    f"--print-to-pdf={out}",
                    source.as_uri(),
                ],
                capture_output=True,
                timeout=PRINT_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise ExportError(
                f"The browser took longer than {PRINT_TIMEOUT:.0f} seconds to print the "
                "PDF. The other formats are unaffected.",
                kind="print_timeout",
            ) from error
        except OSError as error:
            raise ExportError(
                f"The browser could not be started to print the PDF: {error}",
                kind="no_browser",
            ) from error

        if not out.is_file() or not out.stat().st_size:
            detail = finished.stderr.decode("utf-8", "replace").strip().splitlines()
            because = f" It said: {detail[-1]}" if detail else ""
            raise ExportError(
                f"The browser did not produce a PDF (exit {finished.returncode}).{because}",
                kind="print_failed",
            )
        return out.read_bytes()


# Twice the CSS pixel size, so the image holds up in a slide or on paper rather
# than only on the screen it was made on.
PNG_SCALE = 2

# A shorter leash than the print: this is one small page with nothing to fetch,
# and a bundle should not sit on a hung browser for a minute and a half over a
# picture it can do without.
SHOT_TIMEOUT = 45.0

SVG_SIZE_PATTERN = re.compile(r'<svg[^>]*?width="(\d+)" height="(\d+)"')


# Cached because the answer is asked for twice per export and is expensive both
# times: once when the Markdown decides which file to link, and again when the
# bundle writes the file. Identical SVG in, identical PNG out, so the second ask
# is free. Small: at most four diagrams are ever in flight.
@lru_cache(maxsize=8)
def diagram_png(svg: str, binary: str | None = None) -> bytes | None:
    """Rasterise one standalone diagram SVG, or None if it cannot be (F7).

    The same browser that prints the PDF, for the same reason: the alternative is
    an imaging dependency and a second interpretation of the same drawing.

    Unlike the PDF this never raises. The Markdown links the vector where there is
    no raster, so a machine with no browser gets a bundle that is whole rather
    than an export that failed over a picture.
    """
    if not svg:
        return None
    binary = binary or chrome()
    if not binary:
        return None

    size = SVG_SIZE_PATTERN.search(svg)
    if not size:
        return None
    width, height = int(size.group(1)), int(size.group(2))

    with tempfile.TemporaryDirectory(prefix="advisor-png-") as work:
        source = Path(work) / "diagram.svg"
        out = Path(work) / "diagram.png"
        source.write_text(svg, encoding="utf-8")

        try:
            subprocess.run(
                [
                    binary,
                    *PRINT_FLAGS,
                    *sandbox_flags(),
                    # Its own profile, for the reason pdf() gives.
                    f"--user-data-dir={Path(work) / 'profile'}",
                    f"--screenshot={out}",
                    f"--window-size={width},{height}",
                    f"--force-device-scale-factor={PNG_SCALE}",
                    "--hide-scrollbars",
                    source.as_uri(),
                ],
                capture_output=True,
                timeout=SHOT_TIMEOUT,
                check=False,
            )
        except (subprocess.SubprocessError, OSError):
            return None

        if not out.is_file() or not out.stat().st_size:
            return None
        return out.read_bytes()


def diagram_image(option: Option, deliverable: Deliverable) -> str:
    """The image file the Markdown should point at, `.png` where there is one.

    Answered by rasterising rather than by guessing, because whether there is a
    PNG depends on whether this machine has a browser, and a link to a file the
    bundle does not hold is worse than a link to a vector some readers will not
    draw.
    """
    stem = diagram_stem(option, deliverable)
    svg = diagram_svg(option.recommendation.diagram, standalone=True)
    return f"{stem}.png" if diagram_png(svg) else f"{stem}.svg"


# --------------------------------------------------------------------------- #
# The bundle
#
# One format selected downloads that file on its own. Two or more travel as a
# zip, because three separate downloads is three separate save dialogs.
# --------------------------------------------------------------------------- #

FORMATS = ("pdf", "html", "md", "json", "tf")

# The formats that are the document. `tf` is the only one that is not: it is a
# folder of code beside the report rather than another rendering of it, which is
# why the diagram does not travel for a Terraform-only export.
DOCUMENTS = ("pdf", "html", "md", "json")

MIME = {
    "pdf": "application/pdf",
    "html": "text/html; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "json": "application/json",
    "tf": "text/plain; charset=utf-8",
    "svg": "image/svg+xml",
    "png": "image/png",
    "zip": "application/zip",
}


@dataclass
class Written:
    """One file in the bundle, and how to serve it on its own."""

    name: str
    body: bytes
    mime: str


def _diagram_files(deliverable: Deliverable) -> list[Written]:
    """The diagram as files, once per option that has one (F7).

    The vector always, and a PNG beside it wherever there is a browser to
    rasterise with: readers and importers that will not draw an SVG are most of
    why the Markdown wants an image at all. What the Markdown links is decided by
    diagram_image, which asks the same cached question, so the link and the
    bundle cannot disagree.
    """
    files = []
    for option in deliverable.options:
        svg = diagram_svg(option.recommendation.diagram, standalone=True)
        if not svg:
            continue
        stem = diagram_stem(option, deliverable)
        files.append(Written(f"{stem}.svg", svg.encode("utf-8"), MIME["svg"]))
        raster = diagram_png(svg)
        if raster:
            files.append(Written(f"{stem}.png", raster, MIME["png"]))
    return files


def terraform_stem(option: Option, deliverable: Deliverable) -> str:
    """The folder this option's Terraform module goes in.

    Named the way the diagram is: `terraform` on its own for a review, and one
    folder an option for a comparison, so a bundle holding two architectures
    holds two modules rather than one overwritten by the other.
    """
    if not deliverable.compare:
        return "terraform"
    return f"terraform-{slug(option.label, 'option')}"


def _terraform_prefix(option: Option, deliverable: Deliverable) -> str:
    """What every AWS resource name in this option's module starts with.

    A comparison's two modules are two stacks. Left to derive their own prefix
    from the same client they would name their resources the same thing, and
    applying both to one account would be a pile of collisions.
    """
    base = deliverable.meta.client or option.title
    if not deliverable.compare:
        return tf.prefix_for(base)
    letter = slug(option.label, "option")[-1]
    return f"{tf.slug(base, 'advisor', tf.PREFIX_LIMIT - 2)}-{letter}"


def _terraform_files(deliverable: Deliverable) -> list[Written]:
    """A Terraform module an option, each in its own folder in the bundle."""
    files = []
    for option in deliverable.options:
        folder = terraform_stem(option, deliverable)
        for written in tf.files(
            option.recommendation,
            project=deliverable.meta.client or option.title,
            prefix=_terraform_prefix(option, deliverable),
        ):
            files.append(
                Written(
                    f"{folder}/{written.name}",
                    written.text.encode("utf-8"),
                    MIME["md"] if written.name.endswith(".md") else MIME["tf"],
                )
            )
    return files


def write(
    deliverable: Deliverable,
    formats: Sequence[str] = ("pdf", "html", "md"),
    session: str | None = None,
) -> tuple[list[Written], list[str]]:
    """Build every file the dialog asked for, and say what could not be built.

    Returns (files, problems). A format that fails is left out and reported
    rather than failing the export: the other two are still worth having.
    """
    wanted = [name for name in FORMATS if name in set(formats)]
    if not wanted:
        raise ExportError("No format was chosen, so there is nothing to export.", kind="empty")

    files: list[Written] = []
    problems: list[str] = []
    # Kept as the errors they were, not just their sentences: a format that could
    # not be written is a note beside the others, but the only format asked for
    # failing is that error, with its own kind, rather than a generic one.
    failures: list[ExportError] = []

    if "md" in wanted:
        files.append(Written("report.md", markdown(deliverable).encode("utf-8"), MIME["md"]))

    if "html" in wanted:
        files.append(Written("report.html", web_html(deliverable).encode("utf-8"), MIME["html"]))

    if "pdf" in wanted:
        try:
            files.append(Written("report.pdf", pdf(print_html(deliverable)), MIME["pdf"]))
        except ExportError as error:
            failures.append(error)
            problems.append(str(error))

    if "json" in wanted and session:
        files.append(Written("session.json", session.encode("utf-8"), MIME["json"]))

    if "tf" in wanted:
        try:
            files += _terraform_files(deliverable)
        except Exception:
            # Never the whole export. The report is what a client is waiting for,
            # and a scaffold that could not be written is a note beside it.
            log.exception("could not write a Terraform module")
            failure = ExportError(
                "The Terraform module could not be written, so it is not in this bundle.",
                kind="terraform",
            )
            failures.append(failure)
            problems.append(str(failure))

    # The diagram travels beside the Markdown, which references it by name, and
    # is worth having next to the PDF too. It is not worth a zip on its own.
    #
    # The logo does not travel with it. report.html and report.pdf carry it inside
    # them, where it belongs to the document; a brand asset loose in a folder is
    # something to be misused, not something the client asked for.
    documents = [name for name in wanted if name in DOCUMENTS]
    if "md" in wanted or len(documents) > 1:
        files += _diagram_files(deliverable)

    if not files:
        if failures:
            raise failures[0]
        raise ExportError("Nothing could be written.", kind="export")
    return files, problems


def bundle(
    deliverable: Deliverable,
    formats: Sequence[str] = ("pdf", "html", "md"),
    session: str | None = None,
) -> tuple[Written, list[str]]:
    """What the browser downloads: one file, or a zip of the folder.

    The zip carries a folder rather than loose files, so unpacking it into
    Downloads does not scatter report.md and diagram.svg among everything else.
    """
    files, problems = write(deliverable, formats, session)
    stem = basename(deliverable)

    if len(files) == 1:
        only = files[0]
        suffix = only.name.rsplit(".", 1)[-1]
        return Written(f"{stem}.{suffix}", only.body, only.mime), problems

    buffer = io.BytesIO()
    # No compression on what is already compressed, deflate on the text.
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for written in files:
            archive.writestr(f"{stem}/{written.name}", written.body)
    return Written(f"{stem}.zip", buffer.getvalue(), MIME["zip"]), problems
