"""Turn a Claude reply into the shape the web UI renders.

A recommendation arrives as JSON validated against schema.Recommendation, so
there is nothing to guess at: this module's job is to render each field's inline
Markdown to HTML and lay the Mermaid diagram out as a graph. A follow-up answer
is prose, and goes through the small Markdown subset below.

This used to scrape headings, table columns, tick emoji and cost tiers out of
whatever Markdown came back. That code is gone; what is left of it lives in
migrate.py, which is only there to read conversations saved before the change.
"""

import html
import json
import re
from collections.abc import Mapping
from typing import Any

from schema import PILLAR_DOCS, PILLAR_OFFICIAL, Pillar

BULLET_PATTERN = re.compile(r"^\s*[-*+]\s+(.*)$")
ORDERED_PATTERN = re.compile(r"^\s*\d+[.)]\s+(.*)$")
TABLE_DIVIDER = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")
LINE_HEADING_PATTERN = re.compile(r"^(#{1,6})\s*(.+)$")

CODE_PATTERN = re.compile(r"`([^`]+)`")
STRONG_PATTERN = re.compile(r"\*\*([^*]+)\*\*")
EM_PATTERN = re.compile(r"(?<![*\w])\*([^*]+)\*(?!\w)")
LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")

# The front end injects this HTML with innerHTML, so a link is as dangerous as a
# script tag if the scheme is left for the browser to interpret: `javascript:` and
# `data:` URLs both execute. Only these three schemes are rendered as links and
# anything else falls back to plain text. Checked after html.escape(), which can
# neither introduce nor hide a scheme.
SAFE_SCHEMES = ("http://", "https://", "mailto:")


# --------------------------------------------------------------------------- #
# Markdown subset -> HTML
# --------------------------------------------------------------------------- #


def _link(match: re.Match[str]) -> str:
    """Render one Markdown link, dropping any scheme we do not trust."""
    label, url = match.group(1), match.group(2)
    if not url.lower().startswith(SAFE_SCHEMES):
        return label
    return f'<a href="{url}" target="_blank" rel="noopener noreferrer">{label}</a>'


def _inline(text: str) -> str:
    """Render the inline Markdown the advisor actually emits."""
    out = html.escape(text.strip())
    out = CODE_PATTERN.sub(r"<code>\1</code>", out)
    out = STRONG_PATTERN.sub(r"<strong>\1</strong>", out)
    out = EM_PATTERN.sub(r"<em>\1</em>", out)
    return LINK_PATTERN.sub(_link, out)


def md_to_html(markdown: str) -> str:
    """Render a small, predictable subset of Markdown to HTML.

    Covers what the advisor emits: paragraphs, headings, bullet and numbered
    lists, tables, fenced code and the inline marks above. Deliberately not a
    general Markdown implementation.
    """
    lines = (markdown or "").replace("\r\n", "\n").split("\n")
    total = len(lines)
    parts: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []
    list_tag = ""

    def flush_paragraph() -> None:
        if paragraph:
            parts.append(f"<p>{_inline(' '.join(paragraph))}</p>")
            paragraph.clear()

    def flush_list() -> None:
        nonlocal list_tag
        if list_items:
            body = "".join(f"<li>{_inline(item)}</li>" for item in list_items)
            parts.append(f"<{list_tag}>{body}</{list_tag}>")
            list_items.clear()
            list_tag = ""

    def flush_all() -> None:
        flush_paragraph()
        flush_list()

    i = 0
    while i < total:
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):
            flush_all()
            i += 1
            start = i
            while i < total and not lines[i].strip().startswith("```"):
                i += 1
            parts.append(f"<pre><code>{html.escape(chr(10).join(lines[start:i]))}</code></pre>")
            i += 1
            continue

        if not stripped:
            flush_all()
            i += 1
            continue

        heading = LINE_HEADING_PATTERN.match(stripped)
        if heading:
            flush_all()
            level = min(len(heading.group(1)) + 1, 6)
            parts.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            i += 1
            continue

        # A table needs a header row followed by a divider row.
        if stripped.startswith("|") and i + 1 < total and TABLE_DIVIDER.match(lines[i + 1]):
            flush_all()
            header = _split_row(stripped)
            i += 2
            rows: list[list[str]] = []
            while i < total and lines[i].strip().startswith("|"):
                rows.append(_split_row(lines[i].strip()))
                i += 1
            head = "".join(f"<th>{_inline(cell)}</th>" for cell in header)
            body = "".join(
                "<tr>" + "".join(f"<td>{_inline(cell)}</td>" for cell in row) + "</tr>"
                for row in rows
            )
            parts.append(f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")
            continue

        listed = BULLET_PATTERN.match(line)
        tag = "ul"
        if not listed:
            listed = ORDERED_PATTERN.match(line)
            tag = "ol"
        if listed:
            flush_paragraph()
            if list_tag and list_tag != tag:
                flush_list()
            list_tag = tag
            list_items.append(listed.group(1))
            i += 1
            continue

        flush_list()
        paragraph.append(stripped)
        i += 1

    flush_all()
    return "".join(parts)


def _split_row(line: str) -> list[str]:
    """Split a Markdown table row into its cells."""
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


# --------------------------------------------------------------------------- #
# Mermaid -> a graph the front end can draw
# --------------------------------------------------------------------------- #

# `id["Label"]` nodes, edge arrows, and |edge labels| -- the only Mermaid the
# advisor is told to emit.
TOKEN_PATTERN = re.compile(
    r'(?P<node>[A-Za-z0-9_]+)(?:\[\s*"?(?P<label>[^"\]]*)"?\s*\])?'
    r"|(?P<edge>-{2,3}>|-\.-+>|={2,}>|--[xo]|-{3,}|={3,}|~{3,})"
    r"|(?P<text>\|[^|]*\|)"
)
SKIP_PREFIXES = ("flowchart", "graph", "%%")

# `subgraph id["Label"]` and its `end`, which is how a VPC or an availability
# zone boundary arrives (F7). Read here rather than skipped, but bounded: what
# these produce is a box drawn round a group of nodes, and the bounds below are
# what keep it drawable at the size these diagrams are.
SUBGRAPH_PATTERN = re.compile(
    r'^subgraph\s+(?P<id>[A-Za-z0-9_]+)\s*(?:\[\s*"?(?P<label>[^"\]]*)"?\s*\])?\s*$',
    re.IGNORECASE,
)

# A VPC, and availability zones inside it. Nothing here draws a third level, so
# a deeper group is folded into its parent rather than dropped -- losing the
# boundary is a smaller loss than losing the nodes inside it.
MAX_GROUP_LEVELS = 2
MAX_GROUPS = 6
# A box round one node says nothing a node's own outline does not.
MIN_GROUP_MEMBERS = 2
MAX_GROUP_LABEL = 28

# An edge label is drawn into the gutter between two columns, and it reaches the
# SVG as text. The schema asks for two or three words of letters and digits; this
# is what enforces it, the way MAX_QUESTIONS enforces the chips. Anything else is
# dropped rather than escaped: a label is a handful of words, and a stripped one
# still reads.
EDGE_LABEL_CHARS = re.compile(r"[^A-Za-z0-9 ./+%-]")
MAX_EDGE_LABEL = 18


def _edge_label(token: str) -> str:
    """One `|like this|` edge label, reduced to what is safe to draw."""
    text = " ".join(EDGE_LABEL_CHARS.sub("", token.strip("|")).split())
    if len(text) <= MAX_EDGE_LABEL:
        return text
    return text[: MAX_EDGE_LABEL - 1].rstrip() + "…"


def _groups(found: list[dict[str, Any]], members: dict[str, str]) -> list[dict[str, Any]]:
    """The boundaries worth drawing, from the ones the source declared (F7).

    Every rule here is a bound on what the renderers have to be able to draw, and
    every one of them drops a box rather than a node: a diagram with a boundary
    missing still says what the architecture is, and one with a node missing does
    not. Where a box goes, its nodes are handed to the nearest boundary that is
    staying, so a surviving box still contains everything inside it -- an ancestor
    whose own members fell outside it would be a boundary drawn round the wrong
    thing, which is the one thing none of this may do.

    See the renderers for the last of these bounds, the one that depends on how
    tall the finished graph turned out to be.
    """
    # What is drawable at all: two levels deep, and six of them at most.
    kept: dict[str, dict[str, Any]] = {}
    for group in found:
        if len(kept) >= MAX_GROUPS:
            break
        # Too deep to draw. Its members reach the nearest ancestor that is not,
        # by way of nearest() below, so the nodes stay inside a boundary even
        # though this one goes.
        if group["level"] >= MAX_GROUP_LEVELS:
            continue
        kept[group["id"]] = group

    lineage = {group["id"]: group for group in found}

    def nearest(group_id: str | None, among: dict[str, dict[str, Any]]) -> str | None:
        """The closest boundary at or above this one that is being drawn."""
        while group_id and group_id not in among:
            parent = lineage.get(group_id)
            group_id = parent["parent"] if parent else None
        return group_id

    def gather(among: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
        """Each surviving boundary's own nodes, including any handed up to it."""
        held: dict[str, list[str]] = {group_id: [] for group_id in among}
        for node_id, group_id in members.items():
            if not group_id:
                continue
            target = nearest(group_id, among)
            if target is not None:
                held[target].append(node_id)
        return held

    def subtree(group_id: str, held: dict[str, list[str]], among: dict[str, Any]) -> list[str]:
        inside = list(held[group_id])
        for other_id, other in among.items():
            if other["parent"] == group_id:
                inside.extend(subtree(other_id, held, among))
        return inside

    held = gather(kept)
    # Counted over the whole subtree: a VPC whose own nodes all sit inside its
    # availability zones still has everything in it to draw a box round.
    counts = {group_id: len(subtree(group_id, held, kept)) for group_id in kept}

    # A box round a single node says nothing that node's own outline does not --
    # unless it is a zone beside another zone, which is the Multi-AZ pattern
    # itself: a writer alone in one availability zone and a reader and a cache in
    # the next. Dropping the thinner of a pair would leave its node looking as
    # though it sat in no zone at all, so a thin boundary goes only when no
    # sibling of it is staying.
    surviving = {group_id for group_id, count in counts.items() if count >= MIN_GROUP_MEMBERS}
    for group_id, group in kept.items():
        if group_id in surviving or not counts[group_id]:
            continue
        if any(
            other["parent"] == group["parent"] and other_id in surviving
            for other_id, other in kept.items()
        ):
            surviving.add(group_id)

    staying = {group_id: group for group_id, group in kept.items() if group_id in surviving}
    # Gathered again, against what is actually being drawn this time, so anything
    # dropped just now hands its nodes up rather than losing them.
    held = gather(staying)

    drawable = [
        {
            "id": group_id,
            "label": group["label"],
            "level": group["level"],
            "parent": group["parent"] if group["parent"] in staying else None,
            "members": held[group_id],
        }
        for group_id, group in staying.items()
    ]

    # A boundary whose parent did not survive is drawn as one of its own, so it
    # is not padded as though something were around it.
    for group in drawable:
        if group["parent"] is None:
            group["level"] = 0
    return drawable


def parse_diagram(source: str) -> dict | None:
    """Read a Mermaid flowchart into nodes, edges and layout depths.

    Only the `id["Label"]` and `a --> b` forms the advisor is told to emit are
    understood; anything else is passed through as source only.
    """
    if not source:
        return None

    labels: dict[str, str] = {}
    edges: list[dict[str, str]] = []
    order: list[str] = []
    seen: set[str] = set()
    # Open subgraphs, outermost first, and every group read so far. A node joins
    # the innermost group open when it is first seen; a later bare mention of it
    # outside the block does not move it, which is what makes `alb --> asg`
    # after the boundary harmless.
    stack: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    members: dict[str, str] = {}

    for raw_line in source.split("\n"):
        line = raw_line.strip()
        if not line or line.lower().startswith(SKIP_PREFIXES):
            continue

        opened = SUBGRAPH_PATTERN.match(line)
        if opened:
            group_id = opened.group("id")
            label = " ".join((opened.group("label") or group_id).split())
            group = {
                "id": group_id,
                "label": label[:MAX_GROUP_LABEL],
                "level": len(stack),
                "parent": stack[-1]["id"] if stack else None,
            }
            stack.append(group)
            groups.append(group)
            continue

        # An `end` with nothing open is a stray, and ignoring it is kinder than
        # refusing to draw the diagram it appears in. Anything still open at the
        # end of the source closes there.
        if line.lower() == "end":
            if stack:
                stack.pop()
            continue

        if line.lower().startswith("subgraph"):
            continue

        previous: str | None = None
        linked = False
        # The label sits between the arrow and the node it points at, so it is
        # held until that node arrives rather than attached where it was read.
        pending = ""
        # Walk each line as tokens rather than matching whole edges, so a chain
        # (`a --> b --> c`) yields every link in it, not just the first.
        for token in TOKEN_PATTERN.finditer(line):
            if token.group("edge"):
                linked = True
                continue
            if token.group("text"):  # an edge label, |like this|
                pending = _edge_label(token.group("text"))
                continue

            node_id = token.group("node")
            if node_id not in seen:
                seen.add(node_id)
                order.append(node_id)
            label = token.group("label")
            if label:
                labels[node_id] = label.strip()
            # Recorded at first sighting, whether or not a boundary was open:
            # a node declared outside the VPC and merely referred to inside it
            # belongs outside, and only writing down the ones that were inside
            # something would let the later mention claim it.
            if node_id not in members:
                members[node_id] = stack[-1]["id"] if stack else ""
            if linked and previous:
                edges.append({"from": previous, "to": node_id, "label": pending})
            previous, linked, pending = node_id, False, ""

    if not order:
        return {"source": source, "nodes": [], "edges": [], "groups": []}

    # Longest-path depth, so every edge points strictly rightwards.
    depth = dict.fromkeys(order, 0)
    links = [(edge["from"], edge["to"]) for edge in edges if edge["to"] in depth]
    for _ in range(len(order)):
        changed = False
        for source_id, target_id in links:
            candidate = depth[source_id] + 1
            if candidate > depth[target_id]:
                depth[target_id] = candidate
                changed = True
        if not changed:
            break

    targets = {target_id for _, target_id in links}
    nodes = [
        {
            "id": node_id,
            "label": labels.get(node_id, node_id),
            "depth": depth[node_id],
            "entry": node_id not in targets,
        }
        for node_id in order
    ]
    return {
        "source": source,
        "nodes": nodes,
        "edges": edges,
        "groups": _groups(groups, members),
    }


# --------------------------------------------------------------------------- #
# Reading a half-written reply
# --------------------------------------------------------------------------- #


def partial_json(text: str) -> Any | None:
    """Read as much of a truncated JSON document as is safely readable.

    A structured reply arrives a few characters at a time, and the web app draws
    each field as it lands rather than waiting for the closing brace. That means
    reading JSON that is not finished yet.

    The document is cut at the last point where a value was definitely complete
    -- the last comma, or the last closing bracket -- and the containers still
    open at that point are closed. Everything after the cut is discarded, so the
    result is always valid and never half a word. Returns None if nothing is
    complete yet.
    """
    stack: list[str] = []
    safe_at = 0
    safe_stack: list[str] = []
    in_string = False
    escaped = False

    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]":
            if not stack:
                return None  # more closers than openers: not JSON at all
            stack.pop()
            safe_at, safe_stack = index + 1, list(stack)
        elif char == ",":
            # Cut before the comma: what follows it may be half-written.
            safe_at, safe_stack = index, list(stack)

    if not safe_at:
        return None
    try:
        return json.loads(text[:safe_at] + "".join(reversed(safe_stack)))
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Rendering a recommendation
# --------------------------------------------------------------------------- #

EMPTY_PAYLOAD: dict[str, Any] = {
    "structured": True,
    "headline": None,
    "overview": "",
    "region": "",
    "assumptions": [],
    "services": [],
    "notes": [],
    "cost": None,
    "nextQuestions": [],
    "coverage": None,
    "diagram": None,
    "prose": "",
}


def _text(value: Any) -> str:
    """One field of a recommendation, as HTML. Missing fields render as nothing."""
    return _inline(str(value)) if isinstance(value, str) and value.strip() else ""


# How many chips the reader is offered, and how long one may be. Both are here
# rather than in the schema because the schema asks and this enforces: a model
# that writes six questions or a paragraph-long one still has to fit the row.
MAX_QUESTIONS = 3
MAX_QUESTION_CHARS = 80

# The same division of labour for the assumptions panel (F10): the schema asks
# for three or four short sentences, and this is what holds a reply that writes
# eight of them, or one of them at paragraph length, to something a reader can
# scan and edit in place.
#
# The length is generous on purpose, and 140 was the wrong number: a real reply
# writes these at 120 to 140 characters, because an assumption worth arguing with
# carries a figure and a period over which it holds. An over-long one is dropped
# rather than truncated -- half a sentence is not an assumption a reader can
# correct -- which is exactly why the cap must not sit where the model writes.
MAX_ASSUMPTIONS = 5
MAX_ASSUMPTION_CHARS = 240

# A chip that asks for the architecture to be rebuilt is the one thing they must
# never be (F13). There is a button for that, and it is deliberate: an
# architecture that changes because somebody clicked a casual-looking chip is the
# failure the button exists to prevent.
REBUILD_WORDS = re.compile(r"\b(revise|rebuild|redo|regenerate|re-?do)\b", re.IGNORECASE)


def _questions(value: Any) -> list[str]:
    """The follow-up chips, as plain text (F13).

    Plain text, not Markdown: a chip is a button label, and what is sent when it
    is clicked is exactly the words on it. Anything asking for a rebuild is
    dropped rather than rewritten, because a half-understood chip is worse than
    one fewer.
    """
    if not isinstance(value, list):
        return []

    questions: list[str] = []
    for item in value:
        text = " ".join(str(item or "").split())
        if not text or len(text) > MAX_QUESTION_CHARS or REBUILD_WORDS.search(text):
            continue
        if text not in questions:
            questions.append(html.escape(text))
        if len(questions) == MAX_QUESTIONS:
            break
    return questions


def _assumptions(value: Any) -> list[str]:
    """What the architecture takes as read, as plain text (F10).

    Plain text rather than Markdown, for the reason the chips are: each one goes
    into an input the reader can edit, and what comes back out of that input is
    sent to the model as words. Escaped once here and never again -- app.js puts
    this straight into a value attribute, and escaping it twice would show the
    reader an entity.
    """
    if not isinstance(value, list):
        return []

    assumptions: list[str] = []
    for item in value:
        text = " ".join(str(item or "").split())
        if not text or len(text) > MAX_ASSUMPTION_CHARS:
            continue
        if text not in assumptions:
            assumptions.append(html.escape(text))
        if len(assumptions) == MAX_ASSUMPTIONS:
            break
    return assumptions


def _is_note(note: Any) -> bool:
    """Whether there is an observation here worth drawing."""
    return isinstance(note, Mapping) and bool(str(note.get("text") or "").strip())


def _note(note: Mapping[str, Any]) -> dict[str, Any]:
    """One Well-Architected observation, with its pillar's documentation (F6).

    The official name and the link come from schema, keyed on what the model
    filed the note under. They are never read from the reply: the model is not
    asked for a URL, so it cannot supply a wrong one, and app.js writes this
    straight into the DOM. A pillar that is not one of the six -- which
    partial_json can hand us mid-stream -- gets no link rather than a guess.
    """
    name = str(note.get("pillar") or "Note")
    pillar = next((item for item in Pillar if item.value == name), None)
    return {
        "pillar": name,
        "official": PILLAR_OFFICIAL.get(pillar, name) if pillar else name,
        "doc": PILLAR_DOCS.get(pillar, "") if pillar else "",
        "status": "review" if note.get("status") == "review" else "good",
        "text": _text(note.get("text")),
    }


def _coverage(notes: list[dict[str, Any]]) -> dict[str, Any]:
    """Which Well-Architected pillars this reply spoke to, and which it did not (F6).

    Derived here rather than in the browser so that the screen and the exported
    document cannot disagree about a number the reader is being shown. A note
    filed under something that is not a pillar counts towards nothing; parse
    keeps it, because dropping an observation to tidy a tally would be the wrong
    trade.
    """
    named = {note["pillar"] for note in notes}
    missing = [pillar.value for pillar in Pillar if pillar.value not in named]
    return {
        "covered": len(Pillar) - len(missing),
        "total": len(Pillar),
        "missing": missing,
    }


def recommendation_payload(data: Mapping[str, Any]) -> dict[str, Any]:
    """Render a recommendation into the fields the web UI draws.

    Takes a plain mapping rather than a Recommendation, because the same
    rendering is used for the partial snapshots that arrive while the reply is
    still streaming. Every field is treated as optional for that reason; a
    finished reply has been validated against the schema and has them all.
    """
    services = [
        {
            "name": _text(service.get("name")),
            "purpose": _text(service.get("purpose")),
            "reasoning": _text(service.get("reasoning")),
        }
        for service in data.get("services") or []
        if isinstance(service, Mapping) and str(service.get("name") or "").strip()
    ]

    notes = [_note(note) for note in data.get("notes") or [] if _is_note(note)]

    raw_cost = data.get("cost")
    cost = None
    if isinstance(raw_cost, Mapping) and str(raw_cost.get("tier") or "").strip():
        cost = {"tier": str(raw_cost["tier"]), "detail": _text(raw_cost.get("detail"))}

    headline = " ".join(str(data.get("headline") or "").split()).rstrip(".")
    overview = data.get("overview")

    return {
        **EMPTY_PAYLOAD,
        "headline": headline or None,
        "region": str(data.get("region") or ""),
        "overview": md_to_html(overview) if isinstance(overview, str) and overview else "",
        "assumptions": _assumptions(data.get("assumptions")),
        "services": services,
        "notes": notes,
        "cost": cost,
        "nextQuestions": _questions(data.get("next_questions")),
        "coverage": _coverage(notes) if notes else None,
        "diagram": parse_diagram(str(data.get("diagram") or "")),
    }


def prose_payload(markdown: str, diagram_source: str | None = None) -> dict[str, Any]:
    """Render a follow-up answer: Markdown, plus a diagram if it revised one."""
    return {
        **EMPTY_PAYLOAD,
        "structured": False,
        "diagram": parse_diagram(diagram_source or ""),
        "prose": md_to_html(markdown),
    }
