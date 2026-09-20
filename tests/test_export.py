"""export.py: the three deliverables, their naming, and what they leave out.

Nothing here prints a PDF unless it says so: `pdf()` starts a browser, so the one
test that does is marked `slow` alongside the browser suite. Everything the PDF's
content depends on is the print sheet, which is HTML and is checked here.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

import pytest

import branding
import export
from export import MAX_BANDS, Deliverable, Meta, Option
from parse import parse_diagram
from schema import Recommendation
from tests.samples import FULL_JSON, GROUP_COUNT, NODE_COUNT, SERVICE_COUNT

ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
APP_JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

ON = date(2026, 8, 19)


@pytest.fixture
def recommendation() -> Recommendation:
    return Recommendation.model_validate_json(FULL_JSON)


ESTIMATED = 42.5


def svg_width(svg: str) -> int:
    """The width attribute off a rendered diagram, as a number."""
    found = re.search(r'width="(\d+)"', svg)
    assert found, "the SVG carries no width"
    return int(found.group(1))


def estimate_for(
    recommendation: Recommendation, total: float = 1695.0, estimated: float = ESTIMATED
) -> dict:
    """An estimate in the shape pricing.estimate() returns.

    Blended by default, the way a real one is: every service but the last is
    priced from the list, and the last -- CloudFront, in the sample -- carries
    the advisor's own figure. `total` is the whole thing, estimate included.
    """
    priced_total = total - estimated
    services: list[dict[str, Any]] = []
    share = priced_total / max(1, len(recommendation.services) - 1)
    for index, service in enumerate(recommendation.services):
        priced = index < len(recommendation.services) - 1
        figure = round(share, 2) if priced else estimated
        services.append(
            {
                "name": service.name,
                "monthlyUsd": figure,
                "priced": priced,
                "estimated": not priced and estimated > 0,
                "lines": [
                    {
                        "meter": str(line.meter),
                        "priced": priced,
                        "estimated": not priced and estimated > 0,
                        "monthlyUsd": figure,
                        "detail": (
                            f"{line.quantity:g} x $0.0834/hour x 730h"
                            if priced
                            else "not priced here; $42.5000/month is the advisor's own estimate"
                        ),
                    }
                    for line in service.usage
                ],
            }
        )
    lines = [line for service in services for line in service["lines"]]
    return {
        "region": recommendation.region.value,
        "monthlyUsd": total,
        "pricedUsd": round(priced_total, 2),
        "estimatedUsd": estimated,
        "tier": "Medium",
        "claimedTier": recommendation.cost.tier.value,
        "tierGap": 0,
        "priced": True,
        "anyEstimated": estimated > 0,
        "hasFigure": total > 0,
        "unpricedServices": 1,
        "estimatedServices": 1 if estimated > 0 else 0,
        "linesPriced": sum(1 for line in lines if line["priced"]),
        "linesEstimated": sum(1 for line in lines if line["estimated"]),
        "linesTotal": len(lines),
        "pricedAt": "2026-08-19T12:58:04+01:00",
        "problems": [],
        "services": services,
    }


def deliverable_for(
    recommendation: Recommendation,
    priced: bool = True,
    transcript: bool = True,
    diagram_source: bool = False,
    client: str = "Northbridge Mutual",
) -> Deliverable:
    return Deliverable(
        options=[
            Option(
                recommendation=recommendation,
                estimate=estimate_for(recommendation) if priced else None,
                brief="An online shop that falls over every Black Friday.",
            )
        ],
        meta=Meta(
            client=client,
            prepared_by="A. Consultant, Insert Company Name",
            reference="AR-2026-0819-01",
            prepared_on=ON,
            transcript=transcript,
            diagram_source=diagram_source,
        ),
        transcript=[
            ("user", "An online shop that falls over every Black Friday."),
            ("assistant", FULL_JSON),
            ("user", "Is the read replica worth it all year?"),
            ("assistant", "Yes for most of the year."),
        ],
        model="claude-sonnet-5",
    )


def compare_for(recommendation: Recommendation) -> Deliverable:
    """Two options over the same brief, which is what the Compare tab produces."""
    other = recommendation.model_copy(update={"headline": "Serverless, and pay per request"})
    return Deliverable(
        options=[
            Option(
                recommendation=recommendation,
                estimate=estimate_for(recommendation, 1695.0),
                brief="An online shop that falls over every Black Friday.",
                label="Option A",
            ),
            Option(
                recommendation=other,
                estimate=estimate_for(other, 2410.0),
                brief="The same shop, rebuilt serverless.",
                label="Option B",
            ),
        ],
        meta=Meta(client="Northbridge Mutual", reference="AR-2026-0819-02", prepared_on=ON),
        model="claude-sonnet-5",
    )


def wide_compare_for(recommendation: Recommendation, width: int) -> Deliverable:
    """Two to four options over the same brief (F9)."""
    options = []
    for index in range(width):
        letter = chr(ord("A") + index)
        variant = recommendation.model_copy(update={"headline": f"Approach {letter}"})
        options.append(
            Option(
                recommendation=variant,
                estimate=estimate_for(variant, 1000.0 + 500 * index),
                brief=f"The shop, built the {letter} way.",
                label=f"Option {letter}",
            )
        )
    return Deliverable(
        options=options,
        meta=Meta(client="Northbridge Mutual", reference="AR-2026-0819-02", prepared_on=ON),
        model="claude-sonnet-5",
    )


# --------------------------------------------------------------------------- #
# Naming
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Northbridge Mutual", "northbridge-mutual"),
        ("  NHS Trust (Leeds)  ", "nhs-trust-leeds"),
        ("Ampersand & Co.", "ampersand-co"),
        ("", "review"),
        ("!!!", "review"),
    ],
)
def test_a_name_becomes_a_filename_fragment(text, expected):
    assert export.slug(text) == expected


def test_the_bundle_is_named_after_the_client_and_the_day(recommendation):
    assert (
        export.basename(deliverable_for(recommendation))
        == "architecture-review-northbridge-mutual-2026-08-19"
    )


def test_an_unnamed_client_falls_back_to_the_headline(recommendation):
    name = export.basename(deliverable_for(recommendation, client=""))
    assert name.startswith("architecture-review-spread-the-load")
    assert name.endswith("2026-08-19")


def test_a_comparison_is_named_as_options(recommendation):
    assert export.basename(compare_for(recommendation)).startswith("architecture-options-")


def test_the_reference_reads_as_the_brand_writes_them():
    assert export.reference_for(ON) == "AR-2026-0819-01"
    assert export.reference_for(ON, 3) == "AR-2026-0819-03"


def test_a_missing_reference_is_generated_rather_than_left_blank():
    meta = Meta.from_request({}, today=ON)
    assert meta.reference == "AR-2026-0819-01"
    assert meta.long_date == "19 August 2026"


def test_metadata_from_a_request_is_trimmed_and_capped():
    meta = Meta.from_request({"client": "  A  b  ", "preparedBy": "x" * 400}, today=ON)
    assert meta.client == "A b"
    assert len(meta.prepared_by) == export.MAX_META


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #


def cost_section(text: str) -> str:
    """The Markdown cost section, found by name rather than by its number.

    The number moves whenever a section is added above it, and four tests used to
    have to be edited when it did.
    """
    found = re.split(r"^## \d+\. Cost\b.*$", text, maxsplit=1, flags=re.MULTILINE)
    assert len(found) == 2, "no cost section in this document"
    return found[1]


def test_the_markdown_carries_every_section_in_order(recommendation):
    text = export.markdown(deliverable_for(recommendation))
    headings = re.findall(r"^## (.+)$", text, re.MULTILINE)

    assert headings[:6] == [
        "1. Recommendation",
        "2. Reference architecture",
        "3. Services",
        "4. Well-Architected notes",
        "5. What the sizing rests on",
        "6. Cost",
    ]
    assert "Appendix A — The brief, as stated" in headings


def test_the_front_matter_says_what_the_document_is(recommendation):
    text = export.markdown(deliverable_for(recommendation))
    front = text.split("---")[1]

    assert "client: Northbridge Mutual" in front
    assert "date: 2026-08-19" in front
    assert "region: eu-west-2" in front
    # F12: the band is derived from the figure, so the key says band, not tier.
    assert "cost_band: Medium" in front
    assert "monthly_usd: 1695.00" in front
    assert "monthly_usd_estimated: 42.50" in front
    assert "cost_tier:" not in front


def test_every_service_becomes_a_row(recommendation):
    text = export.markdown(deliverable_for(recommendation))
    table = text.split("## 3. Services")[1].split("##")[0]
    # Two header rows plus one per service.
    assert len(table.strip().split("\n")) == SERVICE_COUNT + 2
    assert "| **RDS MySQL (Multi-AZ)** |" in table


def test_a_pipe_in_a_field_cannot_break_a_table(recommendation):
    services = list(recommendation.services)
    services[0] = services[0].model_copy(update={"purpose": "CDN | edge cache"})
    text = export.markdown(
        deliverable_for(recommendation.model_copy(update={"services": services}))
    )
    assert r"CDN \| edge cache" in text


def test_a_note_that_needs_review_says_so(recommendation):
    text = export.markdown(deliverable_for(recommendation))
    assert "⚠ *Needs review.*" in text
    # F6: the pillar is a link, and named the way the framework names it.
    assert "**[Security](https://docs.aws.amazon.com/wellarchitected/" in text


def test_the_estimate_is_priced_in_dollars_and_totalled(recommendation):
    text = export.markdown(deliverable_for(recommendation))
    cost = cost_section(text)

    assert "USD / month" in cost
    assert "| **Total** |  | **$1,695.00** |" in cost
    assert "Assumption, not a measurement." in cost
    # F12: one currency, and it is the one AWS publishes. No sterling anywhere.
    assert "$1,800–3,600/month" in cost
    assert "£" not in text


def test_an_estimated_line_is_named_as_one_in_every_format(recommendation):
    """F12: the reader has to be able to tell which figures to challenge."""
    deliverable = deliverable_for(recommendation)
    text = export.markdown(deliverable)
    page = export.web_html(deliverable)

    assert "*(est.)*" in text
    assert "$42.50 of this total is the advisor's own estimate" in text
    assert "CloudFront" in cost_section(text)

    assert "line__figure--est" in page
    assert "advisor&#x27;s own estimate" in page or "advisor's own estimate" in page


def test_a_fully_priced_architecture_does_not_hedge(recommendation):
    """Nothing was estimated, so nothing should say estimated."""
    deliverable = deliverable_for(recommendation)
    estimate = deliverable.first.estimate
    assert estimate is not None
    estimate.update(
        anyEstimated=False,
        estimatedUsd=0.0,
        pricedUsd=1695.0,
        estimatedServices=0,
        unpricedServices=0,
        linesPriced=estimate["linesTotal"],
        linesEstimated=0,
    )
    for service in estimate["services"]:
        service.update(priced=True, estimated=False)
        for line in service["lines"]:
            line.update(priced=True, estimated=False, detail="1 x $0.0834/hour x 730h")

    cost = cost_section(export.markdown(deliverable))
    assert "*(est.)*" not in cost
    assert "advisor's own estimate" not in cost
    assert "$1,695.00" in cost


def test_the_band_comes_from_the_figure_not_from_the_model(recommendation):
    """The two used to be able to disagree on screen. Now one derives the other."""
    deliverable = deliverable_for(recommendation)
    deliverable.first.estimate["tier"] = "High"

    cost = cost_section(export.markdown(deliverable))
    assert "**High — $1,695 / month**" in cost
    # The model's own read survives as context, clearly labelled and second.
    assert "The advisor's own read, written before anything was looked up" in cost


def test_a_band_a_full_band_from_the_arithmetic_is_flagged(recommendation):
    deliverable = deliverable_for(recommendation)
    estimate = deliverable.first.estimate
    assert estimate is not None
    estimate.update(tier="High", tierGap=2)

    text = export.markdown(deliverable)
    assert "A full band apart means something has been mis-sized" in text
    assert "cost_band_claimed: Medium" in text.split("---")[1]

    assert "caption--warn" in export.web_html(deliverable)


def test_an_architecture_priced_only_by_the_advisor_still_gets_a_document(
    recommendation,
):
    """Nothing from AWS, but a figure all the same, and the document says which."""
    deliverable = deliverable_for(recommendation)
    estimate = deliverable.first.estimate
    assert estimate is not None
    estimate.update(
        priced=False,
        anyEstimated=True,
        hasFigure=True,
        monthlyUsd=300.0,
        pricedUsd=0.0,
        estimatedUsd=300.0,
        tier="Low",
        linesPriced=0,
        linesEstimated=estimate["linesTotal"],
    )
    for service in estimate["services"]:
        service.update(priced=False, estimated=True)
        for line in service["lines"]:
            line.update(priced=False, estimated=True)

    text = export.markdown(deliverable)
    assert "**Low — $300 / month**" in text
    assert "no priced estimate" not in text
    assert "monthly_usd_estimated: 300.00" in text


def test_an_unpriced_architecture_says_why_rather_than_showing_nothing(recommendation):
    text = export.markdown(deliverable_for(recommendation, priced=False))
    assert "no priced estimate" in text
    assert "USD / month" not in text
    # With no figure at all, the band falls back to the advisor's own judgement
    # and the document says so rather than printing nothing.
    assert "cost_band: Medium" in text
    assert "unchecked against any published price" in text
    assert "monthly_usd:" not in text


def test_the_transcript_and_the_diagram_source_are_optional(recommendation):
    without = export.markdown(deliverable_for(recommendation, transcript=False))
    assert "Appendix B" not in without
    assert "Appendix C" not in without

    with_both = export.markdown(deliverable_for(recommendation, diagram_source=True))
    assert "Appendix B — Full transcript" in with_both
    assert "```mermaid" in with_both


def test_the_markdown_points_at_the_files_beside_it(recommendation):
    """With no browser there is no raster, so it points at the vector (F7).

    This used to be `](diagram.png)` against a bundle that never held one -- a
    broken link, pinned by a test. The link is decided from what was written now.
    """
    text = export.markdown(deliverable_for(recommendation))
    assert "](diagram.svg)" in text


def test_the_logo_is_not_loose_in_the_folder(recommendation):
    """It belongs inside report.html and report.pdf, not beside them."""
    text = export.markdown(deliverable_for(recommendation))
    assert export.LOGO not in text

    written, _ = export.bundle(deliverable_for(recommendation), formats=("md", "html"))
    with zipfile.ZipFile(io.BytesIO(written.body)) as archive:
        # The diagram's own raster is allowed to be here; the logo is not.
        loose = [name for name in archive.namelist() if name.endswith(".png")]
    assert not [name for name in loose if "diagram" not in name]

    # Still in the page itself, as a data URI, whatever branding.py points at.
    page = export.web_html(deliverable_for(recommendation))
    assert f"data:{export.LOGO_TYPE};base64," in page


# --------------------------------------------------------------------------- #
# The self-contained page
# --------------------------------------------------------------------------- #


def test_the_web_page_needs_nothing_from_the_network(recommendation):
    """Nothing is *fetched*. The pillar links (F6) are the one exception allowed.

    An anchor is not a request: the page renders identically with no network,
    and the link only goes anywhere when a reader clicks it. What this test is
    really about is that no stylesheet, script, font or image is loaded from
    somewhere else, so those are what it still forbids absolutely.
    """
    page = export.web_html(deliverable_for(recommendation))

    assert "<script" not in page.lower()
    assert "http://" not in page.replace("http://www.w3.org", "")

    # Every remaining outbound URL is an AWS documentation anchor and nothing else.
    for url in re.findall(r"https://[^\"'\s<>]+", page):
        assert url.startswith("https://docs.aws.amazon.com/wellarchitected/"), url
    for attribute in ('src="https://', "@import", "url(https://"):
        assert attribute not in page

    # The fonts and the logo travel inside it.
    assert page.count("data:font/woff2;base64,") == len(export.INLINE_FONTS)
    assert f"data:{export.LOGO_TYPE};base64," in page


def test_the_web_page_leads_with_the_figures_a_client_asks_for(recommendation):
    page = export.web_html(deliverable_for(recommendation))

    assert "Medium" in page
    assert "$1,695" in page
    assert f">{SERVICE_COUNT}<" in page
    assert "eu-west-2" in page
    assert "Northbridge Mutual" in page


def test_the_appendices_fold_away_without_a_script(recommendation):
    page = export.web_html(deliverable_for(recommendation, diagram_source=True))
    assert page.count("<details>") == 2  # the transcript and the diagram source
    assert "<summary>" in page


def test_the_diagram_is_inlined_as_svg(recommendation):
    page = export.web_html(deliverable_for(recommendation))
    assert page.count("<svg") == 1
    assert page.count("<rect") >= NODE_COUNT


def test_a_link_in_a_field_is_checked_before_it_is_written(recommendation):
    """S1: the same check the app applies on screen applies to a file on disk."""
    poisoned = recommendation.model_copy(
        update={"overview": "See [the docs](javascript:alert(1)) for more."}
    )
    page = export.web_html(deliverable_for(poisoned))
    assert "javascript:" not in page
    assert "the docs" in page


def test_a_field_cannot_smuggle_markup_into_the_page(recommendation):
    poisoned = recommendation.model_copy(update={"headline": "<script>alert(1)</script>"})
    page = export.web_html(deliverable_for(poisoned))
    assert "<script>" not in page
    assert "&lt;script&gt;" in page


# --------------------------------------------------------------------------- #
# The print sheet, which is what the PDF is
# --------------------------------------------------------------------------- #


def test_the_sheet_is_a4_with_a_cover_that_bleeds(recommendation):
    sheet = export.print_html(deliverable_for(recommendation))
    assert "@page{size:A4" in sheet.replace(" ", "")
    # A named page, which is how the cover gets no margins while the rest keep theirs.
    assert "@page cover{margin:0}" in sheet.replace("@page cover {", "@page cover{")
    assert "page:cover" in sheet.replace("page: cover", "page:cover")


def test_the_cover_says_who_it_is_for_and_what_it_is_called(recommendation):
    sheet = export.print_html(deliverable_for(recommendation))
    cover = sheet.split('class="sheet"')[0]

    assert "Northbridge Mutual" in cover
    assert "A. Consultant, Insert Company Name" in cover
    assert "19 August 2026" in cover
    assert "AR-2026-0819-01" in cover
    assert "Commercial in confidence" in cover


def test_every_sheet_carries_the_running_header_and_footer(recommendation):
    sheet = export.print_html(deliverable_for(recommendation))
    sheets = sheet.count('<section class="sheet">')

    assert sheets >= 3
    assert sheet.count("sheet__head") == sheets + 1  # the class, plus its rule
    assert sheet.count("AR-2026-0819-01") >= sheets


def test_sections_share_a_sheet_rather_than_one_page_each(recommendation):
    """A short section on its own page leaves most of an A4 sheet empty."""
    built = export.sections(deliverable_for(recommendation))
    groups = export._sheet_groups(built)

    # The assumptions share the cost's sheet rather than taking one for four
    # sentences, so the last group is a pair now too (F10).
    assert [len(group) for group in groups] == [2, 2, 2]
    assert [section.key for section in groups[0]] == ["Recommendation", "Architecture"]


def test_what_must_not_split_across_a_page_says_so(recommendation):
    sheet = export.print_html(deliverable_for(recommendation))
    rules = sheet.split("</style>")[0]
    for selector in (".card", ".note", ".estimate", ".tier", ".brief"):
        assert selector in rules
    assert "break-inside:avoid" in rules.replace("break-inside: avoid", "break-inside:avoid")


# --------------------------------------------------------------------------- #
# The diagram
# --------------------------------------------------------------------------- #


def test_the_diagram_is_laid_out_like_the_app_lays_it_out(recommendation):
    svg = export.diagram_svg(recommendation.diagram)
    assert svg.count("<rect") == NODE_COUNT + GROUP_COUNT
    assert 'rx="8"' in svg
    assert "marker-end" in svg


def test_the_geometry_matches_the_renderer_in_app_js():
    """A5: app.js draws the same graph on screen, and the two must agree."""
    for name, value in (
        ("NODE_HEIGHT", export.NODE_HEIGHT),
        ("NODE_GAP_Y", export.NODE_GAP_Y),
        ("COLUMN_GAP", export.COLUMN_GAP),
        ("NODE_PAD_X", export.NODE_PAD_X),
        ("NODE_MIN_W", export.NODE_MIN_W),
        ("EDGE_LABEL_SIZE", export.EDGE_LABEL_SIZE),
    ):
        assert f"const {name} = {value};" in APP_JS, name


def test_the_palette_is_the_one_the_browser_is_served():
    """A deliverable opens with no stylesheet, so its colours are written in as
    literals -- but they have to be the literals /brand.css carries, or the PDF
    and the page it was printed from would not match (A5)."""
    served = branding.css_root()
    for key, value in export.INK.items():
        assert f"--cs-{key}: {value};" in served, key


def test_a_node_is_styled_by_what_the_service_is():
    ink = branding.palette()

    assert export.node_style({"label": "RDS MySQL"})["fill"] == ink["accent"]
    assert export.node_style({"label": "ECS Fargate"})["fill"] == ink["primary"]
    assert export.node_style({"label": "RDS read replica"})["dashed"] is True
    assert export.node_style({"label": "Users", "entry": True})["fill"] == ink["secondary"]


# --------------------------------------------------------------------------- #
# The boundaries (F7)
# --------------------------------------------------------------------------- #

# A box is drawn round a group of nodes, and the thing that must never happen is
# that it also contains one it does not own. These are the shapes most likely to
# make it happen: a boundary spanning columns that are not next to each other,
# two boundaries over the same columns, and a boundary whose members sit in
# columns of unequal height -- which is the case the old per-column centring
# could not have survived.
ADVERSARIAL = {
    "non-adjacent columns": (
        "flowchart LR\n"
        '  a["A"] --> b["B"]\n'
        '  b --> c["C"]\n'
        '  c --> d["D"]\n'
        '  subgraph vpc["VPC"]\n'
        '    a --> e["E"]\n'
        '    e --> f["F"]\n'
        "  end\n"
    ),
    "two boundaries on the same columns": (
        "flowchart LR\n"
        '  subgraph one["One"]\n'
        '    a["A"] --> b["B"]\n'
        "  end\n"
        '  subgraph two["Two"]\n'
        '    c["C"] --> d["D"]\n'
        "  end\n"
        "  a --> c\n"
    ),
    "columns of unequal height": (
        "flowchart LR\n"
        '  users["Users"] --> alb["ALB"]\n'
        '  users --> cdn["CDN"]\n'
        '  users --> dns["DNS"]\n'
        '  subgraph vpc["VPC"]\n'
        '    alb --> app["App"]\n'
        '    app --> db["DB"]\n'
        "  end\n"
    ),
    # From a real reply. One zone holds a single node and the next holds two,
    # which is the Multi-AZ pattern, and it used to drop the thin one and lose
    # its node from the VPC's box along with it.
    "a thin zone beside a fuller one": (
        "flowchart LR\n"
        '  lam["Lambda"]\n'
        '  subgraph vpc["VPC"]\n'
        '    subgraph aza["AZ A"]\n'
        '      writer["Aurora Writer"]\n'
        "    end\n"
        '    subgraph azb["AZ B"]\n'
        '      reader["Aurora Reader"]\n'
        '      cache["ElastiCache"]\n'
        "    end\n"
        "  end\n"
        "  lam --> writer\n"
        "  writer --> reader\n"
        "  writer --> cache\n"
    ),
    "an availability zone inside a vpc": (
        "flowchart LR\n"
        '  users["Users"] --> alb["ALB"]\n'
        '  subgraph vpc["VPC"]\n'
        '    subgraph az["eu-west-2a"]\n'
        '      alb --> app["App"]\n'
        '      app --> db["DB"]\n'
        "    end\n"
        "  end\n"
    ),
}

BOX = re.compile(
    r'<rect x="(?P<x>[-\d.]+)" y="(?P<y>[-\d.]+)" width="(?P<w>[\d.]+)" '
    r'height="(?P<h>[\d.]+)" rx="(?P<rx>\d+)"'
)


def boxes_of(svg: str) -> tuple[list[dict], list[dict]]:
    """The rects, split into the nodes and the boundaries round them.

    Told apart by their corner radius, which is the one thing that differs
    between them in the markup and is a design decision rather than an accident.
    """
    found = [
        {
            "x": float(box["x"]),
            "y": float(box["y"]),
            "w": float(box["w"]),
            "h": float(box["h"]),
            "rx": int(box["rx"]),
        }
        for box in BOX.finditer(svg)
    ]
    return (
        [box for box in found if box["rx"] == 8],
        [box for box in found if box["rx"] == export.GROUP_RADIUS],
    )


def overlaps(one: dict, other: dict) -> bool:
    return (
        one["x"] < other["x"] + other["w"]
        and one["x"] + one["w"] > other["x"]
        and one["y"] < other["y"] + other["h"]
        and one["y"] + one["h"] > other["y"]
    )


@pytest.mark.parametrize("shape", sorted(ADVERSARIAL))
def test_a_boundary_never_swallows_a_node_from_outside_it(shape):
    """The invariant the band allocation exists to guarantee.

    Nodes are placed on rows allocated for the whole graph rather than centred
    per column, so a boundary's members occupy a contiguous run of them and a
    box drawn round them cannot reach a node that is not one. If this fails, the
    box is being drawn round the wrong thing, which is worse than not drawing it.
    """
    source = ADVERSARIAL[shape]
    plan = export.layout_diagram(source)
    assert plan, shape
    boundaries = plan["groups"]
    assert boundaries, f"nothing was drawn for {shape}"

    inside = {
        str(group["id"]): set(export._group_nodes(group, parse_diagram(source)["groups"]))
        for group in parse_diagram(source)["groups"]
    }
    # Matched by geometry, since the drawn boxes carry no id: every box has to
    # belong to some group, and every node it contains has to be that group's.
    for box in boundaries:
        owners = [
            members
            for label, members in (
                (group["label"], inside[str(group["id"])])
                for group in parse_diagram(source)["groups"]
            )
            if label == box["label"]
        ]
        assert owners, box["label"]
        own = owners[0]
        for node in plan["nodes"]:
            at = plan["layout"][str(node["id"])]
            if str(node["id"]) in own:
                continue
            assert not overlaps(at, box), (
                f"{shape}: {node['id']} is not in {box['label']} but sits inside its box"
            )


def svg_size(svg: str) -> tuple[int, int]:
    """The root element's own width and height."""
    found = re.search(r'<svg[^>]*?width="(\d+)" height="(\d+)"', svg)
    assert found, "the SVG has no size on it"
    return int(found.group(1)), int(found.group(2))


@pytest.mark.parametrize("shape", sorted(ADVERSARIAL))
def test_a_boundary_always_contains_the_nodes_it_owns(shape):
    """The other half of it, and the half a live reply caught me missing.

    Asking only whether a box holds a stranger misses the opposite failure: a
    boundary drawn without one of its own members inside it. That happened where a
    thin availability zone was dropped and its node was not handed up to the VPC,
    so the VPC's box was computed without it and the node was drawn outside the
    boundary it belonged to. Both halves have to hold for a box to mean anything.
    """
    source = ADVERSARIAL[shape]
    plan = export.layout_diagram(source)
    assert plan, shape
    groups = parse_diagram(source)["groups"]
    assert plan["groups"], f"nothing was drawn for {shape}"

    for group in groups:
        box = next((b for b in plan["groups"] if b["label"] == group["label"]), None)
        if box is None:
            continue
        for member in export._group_nodes(group, groups):
            at = plan["layout"].get(member)
            assert at, f"{shape}: {member} was not laid out at all"
            assert (
                at["x"] >= box["x"]
                and at["x"] + at["w"] <= box["x"] + box["w"]
                and at["y"] >= box["y"]
                and at["y"] + at["h"] <= box["y"] + box["h"]
            ), f"{shape}: {member} belongs to {box['label']!r} but is drawn outside its box"


def test_a_thin_zone_beside_a_fuller_one_is_still_drawn():
    """Both zones or neither: one of each is a diagram that misleads.

    A box round a single node says nothing its own outline does not, which is why
    thin boundaries go -- but a writer alone in one availability zone next to a
    reader and a cache in another is the Multi-AZ pattern itself, and dropping the
    thin one leaves the writer looking as though it sat in no zone at all.
    """
    groups = parse_diagram(ADVERSARIAL["a thin zone beside a fuller one"])["groups"]
    assert [group["label"] for group in groups] == ["VPC", "AZ A", "AZ B"]
    assert next(g for g in groups if g["label"] == "AZ A")["members"] == ["writer"]


@pytest.mark.parametrize("shape", sorted(ADVERSARIAL))
def test_every_box_is_inside_the_viewbox(shape):
    """A boundary is padded outwards, so the graph has to have made room for it."""
    svg = export.diagram_svg(ADVERSARIAL[shape])
    width, height = svg_size(svg)
    nodes, boundaries = boxes_of(svg)

    for box in nodes + boundaries:
        assert box["x"] >= 0, shape
        assert box["y"] >= 0, shape
        assert box["x"] + box["w"] <= width, shape
        assert box["y"] + box["h"] <= height, shape


def test_an_outer_boundary_encloses_the_one_inside_it():
    svg = export.diagram_svg(ADVERSARIAL["an availability zone inside a vpc"])
    _nodes, boundaries = boxes_of(svg)
    assert len(boundaries) == 2

    outer, inner = sorted(boundaries, key=lambda box: -box["w"])
    assert outer["x"] <= inner["x"]
    assert outer["y"] <= inner["y"]
    assert outer["x"] + outer["w"] >= inner["x"] + inner["w"]
    assert outer["y"] + outer["h"] >= inner["y"] + inner["h"]
    # And the two are told apart by the dash rather than by a new colour.
    assert 'stroke-dasharray="4 4"' in svg
    assert 'stroke-dasharray="2 4"' in svg


def test_a_boundary_is_drawn_under_the_nodes(recommendation):
    """Painted first, so the nodes and arrows sit on top of the line."""
    svg = export.diagram_svg(recommendation.diagram)
    assert svg.index('stroke-dasharray="4 4"') < svg.index('rx="8"')


def test_a_diagram_whose_boundaries_cannot_be_drawn_still_draws():
    """Degradation is exact, not approximate: the ungrouped layout, to the byte.

    Boundaries put every node on a row allocated for the whole graph, and a graph
    needing more rows than are readable would come out tall and thin. So it drops
    every boundary instead -- and what is left has to be exactly what this engine
    drew before any of this existed, which is what this compares against.
    """
    stacked = [chr(ord("a") + index) for index in range(MAX_BANDS + 1)]
    inside = "\n".join(f'    {name}["{name.upper()}"]' for name in stacked)
    grouped = f'flowchart LR\n  subgraph vpc["VPC"]\n{inside}\n  end\n'
    flat = f"flowchart LR\n{inside}\n"

    # One boundary, but its members all sit in the same column, so it wants a row
    # for each of them.
    assert len(parse_diagram(grouped)["groups"]) == 1
    svg = export.diagram_svg(grouped)
    assert 'stroke-dasharray="4 4"' not in svg
    assert svg == export.diagram_svg(flat)


LABEL_TEXT = re.compile(r'<text x="([\d.]+)"[^>]*font-size="11"[^>]*>([^<]+)</text>')


def test_an_edge_label_never_runs_out_of_its_gutter_and_under_a_node():
    """Being in the gutter is not enough: it has to fit in the gutter.

    A stepped label used to be anchored at the start of the gap and run right, so
    anything longer than the gap was drawn over the node on the far side of it.
    A real reply produced "reads writes" doing exactly that.
    """
    source = (
        "flowchart LR\n"
        '  lam["Lambda Functions"] -->|reads writes every row| db["Aurora Writer"]\n'
        '  lam -->|caches| cache["ElastiCache Redis"]\n'
    )
    plan = export.layout_diagram(source)
    svg = export.diagram_svg(source)
    layout = plan["layout"]

    gutter_left = layout["lam"]["x"] + layout["lam"]["w"]
    gutter_right = layout["db"]["x"]
    for match in LABEL_TEXT.finditer(svg):
        at = float(match.group(1))
        text = match.group(2)
        # Centred, so half the text sits either side of the anchor.
        half = export._text_width(text, export.EDGE_LABEL_SIZE) / 2
        assert at - half >= gutter_left, f"{text!r} runs back under the source node"
        assert at + half <= gutter_right, f"{text!r} runs on under the target node"

    # The long one was cut rather than dropped or drawn over something.
    drawn = [match.group(2) for match in LABEL_TEXT.finditer(svg)]
    assert any(text.endswith("…") for text in drawn), drawn
    assert "caches" in drawn


def test_an_edge_label_sits_in_the_gutter(recommendation):
    """Between the two columns, where there is nothing to collide with."""
    plan = export.layout_diagram(recommendation.diagram)
    layout = plan["layout"]
    svg = export.diagram_svg(recommendation.diagram)
    assert ">https<" in svg

    label = re.search(r'<text x="([\d.]+)"[^>]*font-size="11"[^>]*>https</text>', svg)
    assert label, "the edge label was not drawn"
    at = float(label.group(1))
    assert layout["users"]["x"] + layout["users"]["w"] < at < layout["cf"]["x"]


def test_a_diagram_with_nothing_in_it_draws_nothing():
    assert export.diagram_svg("") == ""
    assert export.diagram_svg("flowchart LR") == ""


def test_the_standalone_file_stands_on_its_own(recommendation):
    svg = export.diagram_svg(recommendation.diagram, standalone=True)
    assert svg.startswith("<svg xmlns=")
    assert 'fill="#FFFFFF"' in svg  # a ground, so it is not drawn on nothing


def test_a_long_label_gets_a_box_wide_enough_for_it():
    narrow = export.diagram_svg('flowchart LR\n  a["S3"]')
    wide = export.diagram_svg('flowchart LR\n  a["Amazon OpenSearch Serverless collection"]')
    assert svg_width(wide) > svg_width(narrow)


# --------------------------------------------------------------------------- #
# The bundle
# --------------------------------------------------------------------------- #


def test_a_self_contained_format_downloads_on_its_own(recommendation):
    written, problems = export.bundle(deliverable_for(recommendation), formats=("html",))

    assert written.name == "architecture-review-northbridge-mutual-2026-08-19.html"
    assert written.mime.startswith("text/html")
    assert problems == []


def test_markdown_travels_with_the_files_it_points_at(recommendation):
    """Choosing Markdown alone still zips: on its own its links are broken."""
    written, _ = export.bundle(deliverable_for(recommendation), formats=("md",))
    assert written.name.endswith(".zip")

    with zipfile.ZipFile(io.BytesIO(written.body)) as archive:
        names = [Path(name).name for name in archive.namelist()]
    assert names == ["report.md", "diagram.svg"]


@pytest.mark.slow
def test_the_bundle_carries_a_raster_where_a_browser_can_make_one(recommendation):
    """F7: the PNG the Markdown has always linked to, and never had.

    Marked slow because it drives a real headless browser, which is what that
    marker means here. The same browser that prints the PDF.
    """
    if not export.chrome():
        pytest.skip("no browser on this machine to rasterise with")
    export.diagram_png.cache_clear()

    written, _ = export.bundle(deliverable_for(recommendation), formats=("md",))
    with zipfile.ZipFile(io.BytesIO(written.body)) as archive:
        names = [Path(name).name for name in archive.namelist()]
        raster = archive.read(next(n for n in archive.namelist() if n.endswith("diagram.png")))
        text = archive.read(next(n for n in archive.namelist() if n.endswith("report.md")))

    assert names == ["report.md", "diagram.svg", "diagram.png"]
    assert raster[:8] == b"\x89PNG\r\n\x1a\n"
    # Twice the CSS width, so it holds up on paper.
    width = int.from_bytes(raster[16:20], "big")
    assert width == svg_size(export.diagram_svg(recommendation.diagram))[0] * export.PNG_SCALE
    # And the link points at it now that it is there.
    assert "](diagram.png)" in text.decode("utf-8")


def test_two_formats_travel_as_one_folder(recommendation):
    written, _ = export.bundle(deliverable_for(recommendation), formats=("html", "md"))
    stem = "architecture-review-northbridge-mutual-2026-08-19"

    assert written.name == f"{stem}.zip"
    with zipfile.ZipFile(io.BytesIO(written.body)) as archive:
        assert all(name.startswith(f"{stem}/") for name in archive.namelist())
        assert f"{stem}/report.html" in archive.namelist()


def test_the_session_file_is_only_written_when_it_is_given(recommendation):
    with_session, _ = export.bundle(
        deliverable_for(recommendation), formats=("md", "json"), session='{"messages": []}'
    )
    with zipfile.ZipFile(io.BytesIO(with_session.body)) as archive:
        assert any(name.endswith("session.json") for name in archive.namelist())

    without, _ = export.bundle(deliverable_for(recommendation), formats=("md", "json"))
    with zipfile.ZipFile(io.BytesIO(without.body)) as archive:
        assert not any(name.endswith("session.json") for name in archive.namelist())


def test_asking_for_no_format_is_refused(recommendation):
    with pytest.raises(export.ExportError) as caught:
        export.bundle(deliverable_for(recommendation), formats=())
    assert caught.value.kind == "empty"


def test_a_pdf_with_no_browser_is_a_problem_not_a_failure(recommendation, monkeypatch):
    """The other formats are still worth having, so the export goes ahead."""
    monkeypatch.setattr(export, "chrome", lambda: None)
    written, problems = export.bundle(deliverable_for(recommendation), formats=("pdf", "md"))

    assert len(problems) == 1
    assert "No browser was found" in problems[0]
    with zipfile.ZipFile(io.BytesIO(written.body)) as archive:
        assert not any(name.endswith(".pdf") for name in archive.namelist())
        assert any(name.endswith("report.md") for name in archive.namelist())


def test_a_pdf_alone_with_no_browser_has_nothing_to_write(recommendation, monkeypatch):
    monkeypatch.setattr(export, "chrome", lambda: None)
    with pytest.raises(export.ExportError) as caught:
        export.bundle(deliverable_for(recommendation), formats=("pdf",))
    assert "No browser" in str(caught.value)


# --------------------------------------------------------------------------- #
# The transcript appendix
# --------------------------------------------------------------------------- #


def test_an_architecture_turn_is_recorded_in_words_not_in_json(recommendation):
    """A stored architecture is JSON, and a client should never be shown it.

    Appendix B used to print the reply verbatim, which for a structured turn is
    several thousand characters of one-line JSON. It now says what happened.
    """
    deliverable = Deliverable(
        options=[Option(recommendation=recommendation, brief="A shop")],
        meta=Meta(client="Northbridge Mutual", prepared_on=ON, transcript=True),
        transcript=[
            ("user", "An online shop that falls over on Black Friday"),
            ("assistant", FULL_JSON),
            ("user", "Would a read replica help?"),
            ("assistant", "Yes, for most of the year."),
        ],
    )
    text = export.markdown(deliverable)
    appendix = text.split("Appendix B")[1]

    assert '"headline"' not in appendix
    assert '"services"' not in appendix
    assert "Recommended an architecture: Spread the load" in appendix
    assert "7 services in eu-west-2" in appendix
    # A prose turn is untouched.
    assert "Yes, for most of the year." in appendix


def test_the_same_is_true_of_the_web_page(recommendation):
    deliverable = Deliverable(
        options=[Option(recommendation=recommendation, brief="A shop")],
        meta=Meta(client="Northbridge Mutual", prepared_on=ON, transcript=True),
        transcript=[
            ("user", "An online shop"),
            ("assistant", FULL_JSON),
        ],
    )
    page = export.web_html(deliverable)

    assert '"headline"' not in page.split("</style>")[1]
    assert "Recommended an architecture" in page


@pytest.mark.parametrize(
    "text",
    [
        "Just some prose.",
        "{not json at all",
        '{"headline": "x"}',  # a dict, but not a recommendation
        "",
    ],
)
def test_a_turn_that_is_not_an_architecture_is_left_alone(text):
    assert export._spoken("assistant", text) == " ".join(text.split())


def test_a_client_turn_is_never_rewritten():
    """Even if the client pasted JSON at us, that is what they said."""
    assert export._spoken("user", FULL_JSON) == " ".join(FULL_JSON.split())


# --------------------------------------------------------------------------- #
# The Terraform module in the bundle (F4)# --------------------------------------------------------------------------- #
# The Terraform module in the bundle (F4)
#
# terraform.py is tested on its own in test_terraform.py, including against a
# real `terraform validate`. What is checked here is only what export.py decides:
# which folder it goes in, what travels with it, and what does not.
# --------------------------------------------------------------------------- #


def test_the_terraform_module_travels_in_a_folder_of_its_own(recommendation):
    written, problems = export.bundle(deliverable_for(recommendation), formats=("tf",))
    stem = "architecture-review-northbridge-mutual-2026-08-19"

    assert problems == []
    assert written.name == f"{stem}.zip"
    with zipfile.ZipFile(io.BytesIO(written.body)) as archive:
        names = archive.namelist()

    assert f"{stem}/terraform/main.tf" in names
    assert f"{stem}/terraform/README.md" in names
    # Nothing loose in the folder the report would occupy.
    assert all(name.startswith(f"{stem}/terraform/") for name in names)


def test_a_terraform_only_export_does_not_carry_the_diagram(recommendation):
    """It is a module, not a document: nothing in it points at a picture."""
    written, _ = export.bundle(deliverable_for(recommendation), formats=("tf",))

    with zipfile.ZipFile(io.BytesIO(written.body)) as archive:
        assert not any(name.endswith(".svg") for name in archive.namelist())


def test_the_module_travels_beside_the_report_when_both_were_asked_for(recommendation):
    written, _ = export.bundle(deliverable_for(recommendation), formats=("md", "tf"))

    with zipfile.ZipFile(io.BytesIO(written.body)) as archive:
        names = archive.namelist()

    assert any(name.endswith("/report.md") for name in names)
    assert any(name.endswith("/diagram.svg") for name in names)
    assert any(name.endswith("/terraform/variables.tf") for name in names)


def test_the_module_is_named_after_the_client_the_document_is_for(recommendation):
    written, _ = export.bundle(deliverable_for(recommendation), formats=("tf",))

    with zipfile.ZipFile(io.BytesIO(written.body)) as archive:
        variables = archive.read(
            next(n for n in archive.namelist() if n.endswith("variables.tf"))
        ).decode("utf-8")

    assert 'default     = "northbridge"' in variables
    assert 'default     = "Northbridge Mutual"' in variables


def test_a_review_with_no_client_is_named_after_the_architecture(recommendation):
    written, _ = export.bundle(deliverable_for(recommendation, client=""), formats=("tf",))

    with zipfile.ZipFile(io.BytesIO(written.body)) as archive:
        variables = archive.read(
            next(n for n in archive.namelist() if n.endswith("variables.tf"))
        ).decode("utf-8")

    # The headline, slugged: "Spread the load, cache the reads, ..."
    assert 'default     = "spread-the"' in variables


def name_prefix_default(variables: str) -> str:
    """The default of the module's own name_prefix variable."""
    found = re.search(r'variable "name_prefix".*?default     = "([^"]+)"', variables, re.DOTALL)
    assert found, "variables.tf declares no name_prefix default"
    return found.group(1)


def test_a_comparison_writes_a_module_an_option(recommendation):
    written, _ = export.bundle(compare_for(recommendation), formats=("tf",))

    with zipfile.ZipFile(io.BytesIO(written.body)) as archive:
        names = archive.namelist()
        prefixes = sorted(
            name_prefix_default(archive.read(name).decode("utf-8"))
            for name in names
            if name.endswith("variables.tf")
        )

    assert any("/terraform-option-a/main.tf" in name for name in names)
    assert any("/terraform-option-b/main.tf" in name for name in names)
    # Two stacks, so two prefixes: applying both to one account is not a pile of
    # resources fighting over the same names.
    assert prefixes == ["northbridg-a", "northbridg-b"]


def test_a_module_that_cannot_be_written_is_a_problem_not_a_failure(recommendation, monkeypatch):
    """The report is what a client is waiting for."""
    monkeypatch.setattr(export.tf, "files", lambda *a, **k: 1 / 0)
    written, problems = export.bundle(deliverable_for(recommendation), formats=("md", "tf"))

    assert len(problems) == 1
    assert "Terraform module could not be written" in problems[0]
    with zipfile.ZipFile(io.BytesIO(written.body)) as archive:
        assert any(name.endswith("report.md") for name in archive.namelist())
        assert not any(name.endswith(".tf") for name in archive.namelist())


def test_a_module_alone_that_cannot_be_written_is_the_failure(recommendation, monkeypatch):
    monkeypatch.setattr(export.tf, "files", lambda *a, **k: 1 / 0)
    with pytest.raises(export.ExportError) as caught:
        export.bundle(deliverable_for(recommendation), formats=("tf",))

    assert caught.value.kind == "terraform"


def test_the_dialog_offers_exactly_the_formats_the_server_writes():
    """One list in app.js, one in export.py, and they have to agree."""
    offered = re.findall(r"^    id: '([a-z]+)',$", APP_JS, re.MULTILINE)

    assert offered == list(export.FORMATS)


def test_the_dialog_zips_the_same_formats_the_server_zips():
    """Markdown and Terraform are folders, so choosing either alone still zips."""
    declared = re.search(r"const FOLDER_FORMATS = \[(.*?)\];", APP_JS, re.DOTALL)
    assert declared, "app.js declares no FOLDER_FORMATS"
    folders = re.findall(r"'([a-z]+)'", declared.group(1))

    assert sorted(folders) == ["md", "tf"]
    for name in folders:
        written, _ = export.bundle(deliverable_for(recommendation_for()), formats=(name,))
        assert written.name.endswith(".zip"), name


def recommendation_for() -> Recommendation:
    """The sample, for the tests that are not given the fixture."""
    return Recommendation.model_validate_json(FULL_JSON)


# --------------------------------------------------------------------------- #
# A comparison
# --------------------------------------------------------------------------- #


def test_a_comparison_pairs_the_options_rather_than_stacking_them(recommendation):
    text = export.markdown(compare_for(recommendation))

    assert "## 1. Side by side" in text
    assert "| **Approach** |" in text
    assert "| Option A | Option B |" in text.replace("|  ", "| ")


def test_a_comparison_prices_both_columns_from_one_table(recommendation):
    text = export.markdown(compare_for(recommendation))
    cost = text.split("## 2. Cost, line by line")[1]

    assert "| **Total per month** | **$1,695.00** | **$2,410.00** |" in cost


def test_a_comparison_does_not_invent_a_recommendation(recommendation):
    """Neither option is marked preferred, and the document says so plainly."""
    text = export.markdown(compare_for(recommendation))
    verdict = text.split("## 3. Recommendation")[1]

    assert "none is marked as preferred" in verdict
    assert "$715.00 a month less" in verdict


def test_orange_marks_the_recommended_option_and_nothing_else(recommendation):
    """Two accented cost panels side by side read as two recommendations."""
    neither = export.print_html(compare_for(recommendation)).split("</style>")[1]
    assert neither.count('class="tier tier--plain"') == 2
    assert 'class="tier"' not in neither

    compare = compare_for(recommendation)
    marked = Deliverable(
        options=[
            Option(
                recommendation=compare.options[0].recommendation,
                estimate=compare.options[0].estimate,
                label="Option A",
                recommended=True,
            ),
            compare.options[1],
        ],
        meta=compare.meta,
    )
    page = export.print_html(marked).split("</style>")[1]
    assert page.count('class="tier tier--plain"') == 1
    assert 'class="tier"' in page


def test_a_single_review_has_one_cost_element_and_no_panel(recommendation):
    """F12: the panel and the table were the same architecture costed twice.

    A comparison still uses the panel, because it holds the two columns' rows
    aligned and marks the recommended option. A single review has nothing to
    align against, so the figure lives in the one estimate element.
    """
    body = export.web_html(deliverable_for(recommendation)).split("</style>")[1]
    assert 'class="tier"' not in body
    assert "tier--plain" not in body
    assert body.count('class="estimate"') == 1
    # One figure, and the band beside it derived from that figure.
    assert "$1,695" in body
    assert "Band: Medium, derived from the figure" in body


def test_a_preferred_option_is_named_once(recommendation):
    compare = compare_for(recommendation)
    preferred = compare.options[0]
    marked = Deliverable(
        options=[
            Option(
                recommendation=preferred.recommendation,
                estimate=preferred.estimate,
                label="Option A",
                recommended=True,
            ),
            compare.options[1],
        ],
        meta=compare.meta,
    )
    text = export.markdown(marked)
    assert "We would build **Option A**" in text
    assert "Option A · recommended" in text


def test_a_comparison_names_its_diagrams_apart(recommendation):
    written, _ = export.bundle(compare_for(recommendation), formats=("html", "md"))
    with zipfile.ZipFile(io.BytesIO(written.body)) as archive:
        names = [Path(name).name for name in archive.namelist()]
    assert "diagram-option-a.svg" in names
    assert "diagram-option-b.svg" in names


# --------------------------------------------------------------------------- #
# The PDF itself, which needs a browser
# --------------------------------------------------------------------------- #


@pytest.mark.slow
def test_the_pdf_is_a_pdf_of_several_pages(recommendation):
    if not export.chrome():
        pytest.skip("no browser to print with")

    written, problems = export.bundle(deliverable_for(recommendation), formats=("pdf",))
    assert problems == []
    assert written.name.endswith(".pdf")
    assert written.body.startswith(b"%PDF-")
    # A cover, three sheets of report and an appendix, at the least.
    counted = re.search(rb"/Type\s*/Pages.*?/Count\s+(\d+)", written.body, re.S)
    assert counted, "the PDF carries no page count"
    pages = int(counted.group(1))
    assert pages >= 4


# --------------------------------------------------------------------------- #
# Well-Architected notes, as the app draws them
# --------------------------------------------------------------------------- #


def test_a_pillar_is_named_in_full_the_way_the_framework_names_it(recommendation):
    """Not "REL", and not the app's shorthand either (F6).

    A client checking a review against the framework should not have to work out
    that "Cost" meant Cost Optimization, so the document prints AWS's own name
    and links to AWS's own page for it.
    """
    page = export.web_html(deliverable_for(recommendation))
    body = page.split("</style>")[1]

    for pillar in ("Reliability", "Security", "Performance Efficiency", "Cost Optimization"):
        assert 'class="pillar" href="https://docs.aws.amazon.com/' in body
        assert f">{pillar}</a>" in body
    for abbreviation in (">REL<", ">SEC<", ">PERF<", ">OPS<"):
        assert abbreviation not in body


def test_the_document_says_how_many_pillars_were_covered(recommendation):
    """F6: the sample answers four of six, and a reader is told which two it did not."""
    text = export.markdown(deliverable_for(recommendation))
    assert "4 of 6 pillars are covered above." in text
    assert "Not addressed: Operational Excellence, Sustainability." in text

    page = export.web_html(deliverable_for(recommendation))
    assert "4 of 6 pillars covered." in page


def test_a_note_carries_the_status_colour_the_app_gives_it(recommendation):
    rules = export.web_html(deliverable_for(recommendation)).split("</style>")[0]

    # Green where the architecture handles it, amber where it needs a look, and
    # the amber row tint that goes with it. The values are app.css's own.
    assert f"color:{export.INK['success']}" in rules.replace(" ", "")
    assert "rgba(31,157,85,0.08)" in rules.replace(" ", "")
    assert ".note--review .pillar" in rules
    assert "rgba(217,119,6,0.05)" in rules.replace(" ", "")


def test_the_pillar_styles_match_the_stylesheet_they_came_from():
    """A5: .note and .pillar exist in app.css, and the export copies them."""
    for selector in (".pillar {", ".note--review .pillar {", ".note--review {"):
        assert selector in CSS, selector


# --------------------------------------------------------------------------- #
# Conversations from before the schema gained its pricing fields
# --------------------------------------------------------------------------- #


def test_a_review_that_stated_no_region_says_nothing_about_one(recommendation):
    """Older conversations have no region, and a document must not invent one."""
    option = Option(
        recommendation=recommendation,
        brief="A shop that falls over on Black Friday.",
        stated_region="",
    )
    deliverable = Deliverable(options=[option], meta=Meta(client="Leeds", prepared_on=ON))

    assert option.region == ""
    text = export.markdown(deliverable)
    assert "region:" not in text
    assert "eu-west-2" not in text

    body = export.web_html(deliverable).split("</style>")[1]
    assert ">Region<" not in body


def test_a_stated_region_is_still_printed(recommendation):
    deliverable = deliverable_for(recommendation)
    assert deliverable.first.region == "eu-west-2"
    assert "region: eu-west-2" in export.markdown(deliverable)


def test_a_review_with_no_headline_is_titled_by_its_brief(recommendation):
    """Conversations migrated from Markdown have no headline: R4 came later."""
    bare = recommendation.model_copy(update={"headline": ""})
    option = Option(
        recommendation=bare,
        brief="A serverless IoT data pipeline feeding a live ops dashboard.",
    )
    deliverable = Deliverable(options=[option], meta=Meta(prepared_on=ON))

    assert deliverable.subject == "A serverless IoT data pipeline feeding a live ops dashboard"
    assert "# Architecture review — A serverless IoT" in export.markdown(deliverable)


def test_a_review_with_neither_headline_nor_brief_still_has_a_title(recommendation):
    bare = recommendation.model_copy(update={"headline": ""})
    deliverable = Deliverable(options=[Option(recommendation=bare)], meta=Meta(prepared_on=ON))
    assert deliverable.subject == "Architecture review"


def test_a_long_brief_is_cut_rather_than_run_as_a_heading(recommendation):
    bare = recommendation.model_copy(update={"headline": ""})
    option = Option(recommendation=bare, brief="word " * 60)
    assert len(option.title) <= 61
    assert option.title.endswith("…")


# --------------------------------------------------------------------------- #
# Numbering
# --------------------------------------------------------------------------- #


def test_a_comparison_numbers_only_the_sections_it_has(recommendation):
    """Neither option priced means no cost table, and no gap where it would be."""
    unpriced = Deliverable(
        options=[
            Option(compare_for(recommendation).options[0].recommendation, label="Option A"),
            Option(compare_for(recommendation).options[1].recommendation, label="Option B"),
        ],
        meta=Meta(prepared_on=ON),
    )
    headings = re.findall(r"^## (\d+)\. (.+)$", export.markdown(unpriced), re.MULTILINE)

    assert [number for number, _ in headings] == ["1", "2"]
    assert [title for _, title in headings] == ["Side by side", "Recommendation"]


def test_a_priced_comparison_numbers_all_three(recommendation):
    headings = re.findall(
        r"^## (\d+)\. (.+)$", export.markdown(compare_for(recommendation)), re.MULTILINE
    )
    assert [number for number, _ in headings] == ["1", "2", "3"]


# --------------------------------------------------------------------------- #
# More than two options (F9)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("width", [2, 3, 4])
def test_a_comparison_of_any_width_writes_every_column(recommendation, width):
    deliverable = wide_compare_for(recommendation, width)
    text = export.markdown(deliverable)
    page = export.web_html(deliverable)

    side = text.split("## 1. Side by side")[1].split("## 2.")[0]
    header = next(line for line in side.split("\n") if line.startswith("|"))
    # One leading label column, then one an option.
    assert header.count("|") == width + 2

    for index in range(width):
        letter = chr(ord("A") + index)
        assert f"Option {letter}" in text
        assert f"Approach {letter}" in text
        assert f"Approach {letter}" in page


@pytest.mark.parametrize("width", [3, 4])
def test_a_wide_comparison_gets_a_grid_that_fits_it(recommendation, width):
    page = export.web_html(wide_compare_for(recommendation, width))

    assert f"columns--{width}" in page
    assert f"twin--{width}" in page
    assert f".columns--{width}{{" in page.split("</style>")[0]


@pytest.mark.parametrize("width", [3, 4])
def test_the_cost_table_totals_every_column(recommendation, width):
    text = export.markdown(wide_compare_for(recommendation, width))
    cost = text.split("## 2. Cost, line by line")[1]
    total = next(line for line in cost.split("\n") if "Total per month" in line)

    assert total.count("|") == width + 2
    for index in range(width):
        assert f"${1000 + 500 * index:,.2f}" in total


def test_a_wide_comparison_names_the_cheapest_rather_than_a_pair(recommendation):
    """With four options "the second" means nothing, so the verdict names them."""
    verdict = export.markdown(wide_compare_for(recommendation, 4)).split("## 3. Recommendation")[1]

    assert "All 4 options were reviewed" in verdict
    assert "Option A is the least expensive" in verdict
    assert "$1,500.00 a month less than Option D" in verdict


def test_a_wide_comparison_exports_a_terraform_module_an_option(recommendation):
    """Applying four modules to one account must not be a pile of collisions."""
    deliverable = wide_compare_for(recommendation, 4)
    written = export._terraform_files(deliverable)
    files = {item.name: item.body.decode("utf-8") for item in written}

    folders = {name.split("/")[0] for name in files}
    assert folders == {
        "terraform-option-a",
        "terraform-option-b",
        "terraform-option-c",
        "terraform-option-d",
    }

    # Four modules, four resource-name prefixes: applying all four to one
    # account must not be a pile of collisions.
    prefixes = set()
    for folder in sorted(folders):
        block = files[f"{folder}/variables.tf"].split('variable "name_prefix"')[1]
        prefixes.add(next(line for line in block.split("\n") if "default" in line))
    assert len(prefixes) == 4


def test_the_regime_a_review_was_built_for_is_printed(recommendation):
    """F5: recorded, not claimed. The document says what it was asked to satisfy."""
    deliverable = Deliverable(
        options=[Option(recommendation=recommendation, estimate=estimate_for(recommendation))],
        meta=Meta(client="An NHS trust", prepared_on=ON, compliance="nhs-dspt"),
    )
    page = export.web_html(deliverable)

    assert "NHS DSPT" in page
    assert "Built for" in page


def test_a_review_that_named_no_regime_says_nothing_about_one(recommendation):
    page = export.web_html(deliverable_for(recommendation))
    assert "Built for" not in page
