"""parse.py: the Markdown subset, the recommendation renderer and the diagram."""

import json
from pathlib import Path

import pytest

from advisor import call_claude, extract_mermaid
from parse import (
    MAX_ASSUMPTION_CHARS,
    MAX_EDGE_LABEL,
    MAX_GROUP_LABEL,
    md_to_html,
    parse_diagram,
    partial_json,
    prose_payload,
    recommendation_payload,
)
from tests.conftest import FakeClient, cited, fake_response
from tests.samples import (
    ASSUMPTIONS,
    EDGE_COUNT,
    FOLLOW_UP,
    FULL_JSON,
    GROUP_COUNT,
    NODE_COUNT,
    NOTE_COUNT,
    RECOMMENDATION,
    SERVICE_COUNT,
)


@pytest.fixture(scope="module")
def rendered():
    """The golden recommendation, rendered the way server.py renders one."""
    return recommendation_payload(RECOMMENDATION)


# --------------------------------------------------------------------------- #
# The full recommendation
# --------------------------------------------------------------------------- #


def test_recommendation_is_structured(rendered):
    assert rendered["structured"] is True


def test_headline_is_plain_text(rendered):
    assert (
        rendered["headline"] == "Spread the load, cache the reads, scale back down after the sale"
    )


def test_overview_is_rendered_html(rendered):
    assert rendered["overview"].startswith("<p>")
    assert "<strong>CDN</strong>" in rendered["overview"]


def test_services_are_rendered_in_order(rendered):
    services = rendered["services"]
    assert len(services) == SERVICE_COUNT
    assert services[0] == {
        "name": "CloudFront",
        "purpose": "Content delivery network",
        "reasoning": "Serves images, CSS and JS from edge locations.",
    }
    assert services[-1]["name"] == "RDS Read Replica"


def test_notes_carry_pillar_and_status(rendered):
    notes = rendered["notes"]
    assert len(notes) == NOTE_COUNT
    assert [note["status"] for note in notes] == ["good", "good", "good", "review", "review"]
    assert [note["pillar"] for note in notes] == [
        "Reliability",
        "Reliability",
        "Performance",
        "Security",
        "Cost",
    ]


def test_a_pillar_carries_its_official_name_and_documentation(rendered):
    """F6: the app's short names are not the framework's, so both travel."""
    performance = next(note for note in rendered["notes"] if note["pillar"] == "Performance")
    assert performance["official"] == "Performance Efficiency"
    assert performance["doc"] == (
        "https://docs.aws.amazon.com/wellarchitected/latest/"
        "performance-efficiency-pillar/welcome.html"
    )
    # Reliability is one of the three whose short name is already the real one.
    reliability = next(note for note in rendered["notes"] if note["pillar"] == "Reliability")
    assert reliability["official"] == "Reliability"


def test_a_pillar_the_model_invented_gets_no_link():
    """partial_json can hand us a half-written pillar name mid-stream."""
    payload = recommendation_payload(
        {"notes": [{"pillar": "Relia", "status": "good", "text": "Half a word."}]}
    )
    note = payload["notes"][0]
    assert note["pillar"] == "Relia"
    assert note["doc"] == ""
    assert note["official"] == "Relia"


def test_coverage_counts_the_pillars_that_were_spoken_to(rendered):
    """F6: the sample answers four of the six, and says which two it missed."""
    assert rendered["coverage"] == {
        "covered": 4,
        "total": 6,
        "missing": ["Operations", "Sustainability"],
    }


def test_coverage_is_nothing_until_there_is_a_note():
    assert recommendation_payload({"headline": "Still writing"})["coverage"] is None


def test_the_follow_up_chips_come_from_the_reply(rendered):
    """F13: the chips are about this architecture, not a hardcoded three."""
    assert rendered["nextQuestions"] == [
        "Do we still need the replica after peak?",
        "What happens if that data centre fails?",
        "Who renews the HTTPS certificate?",
    ]


def test_a_chip_asking_to_rebuild_is_dropped():
    """Rebuilding is a deliberate button, never a casual-looking chip."""
    payload = recommendation_payload(
        {
            "next_questions": [
                "Revise the architecture for us",
                "What are the risks?",
                "Rebuild this without the cache",
                "Regenerate it as serverless",
            ]
        }
    )
    assert payload["nextQuestions"] == ["What are the risks?"]


def test_chips_are_capped_and_deduplicated():
    payload = recommendation_payload(
        {
            "next_questions": [
                "One?",
                "One?",
                "Two?",
                "Three?",
                "Four?",
                "x" * 200,
            ]
        }
    )
    assert payload["nextQuestions"] == ["One?", "Two?", "Three?"]


def test_a_chip_is_plain_text_not_markup():
    """The text on a chip is exactly what gets sent when it is clicked."""
    payload = recommendation_payload({"next_questions": ["<script>alert(1)</script>"]})
    assert payload["nextQuestions"] == ["&lt;script&gt;alert(1)&lt;/script&gt;"]


def test_the_assumptions_come_from_the_reply(rendered):
    """F10: what the sizing rests on, stated rather than buried in the prose."""
    assert rendered["assumptions"] == ASSUMPTIONS


def test_assumptions_are_capped_and_deduplicated():
    payload = recommendation_payload(
        {
            "assumptions": [
                "  Two   thousand orders a day. ",
                "Two thousand orders a day.",
                "Forty GB of history.",
                "Busy from 8am.",
                "Nothing overnight.",
                "One user.",
                "Two users.",
                "x" * (MAX_ASSUMPTION_CHARS + 1),
            ]
        }
    )
    assert payload["assumptions"] == [
        "Two thousand orders a day.",
        "Forty GB of history.",
        "Busy from 8am.",
        "Nothing overnight.",
        "One user.",
    ]


def test_the_editable_length_is_the_same_on_both_sides():
    """The panel offers a box; this is what the server will keep of what goes in it.

    A shorter box in the browser would hand the reader an assumption they cannot
    edit without first deleting some of it, and a longer one would have its tail
    quietly dropped on the way back. Same pattern as the geometry constants in
    tests/test_export.py, and the same reason.
    """
    app_js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    assert f"const MAX_ASSUMPTION_CHARS = {MAX_ASSUMPTION_CHARS};" in app_js


def test_an_assumption_is_escaped_once_because_it_goes_into_an_input():
    """It is drawn as text and round-trips through a value attribute (F10)."""
    payload = recommendation_payload({"assumptions": ['<b>5 GB</b> a "month"']})
    assert payload["assumptions"] == ["&lt;b&gt;5 GB&lt;/b&gt; a &quot;month&quot;"]


def test_cost_tier_and_detail(rendered):
    assert rendered["cost"]["tier"] == "Medium"
    assert rendered["cost"]["detail"].startswith("$1,800–3,600/month during peak")


def test_diagram_is_laid_out_left_to_right(rendered):
    diagram = rendered["diagram"]
    assert len(diagram["nodes"]) == NODE_COUNT
    assert len(diagram["edges"]) == EDGE_COUNT

    depths = {node["id"]: node["depth"] for node in diagram["nodes"]}
    assert depths["users"] == 0
    assert depths["cf"] == 1
    # s3 and alb both hang off CloudFront, so they share a column.
    assert depths["s3"] == depths["alb"] == 2
    assert depths["replica"] == 6
    # Every edge points strictly rightwards.
    assert all(depths[edge["from"]] < depths[edge["to"]] for edge in diagram["edges"])

    entries = [node["id"] for node in diagram["nodes"] if node["entry"]]
    assert entries == ["users"]
    assert diagram["source"].startswith("flowchart LR")


def test_a_recommendation_has_no_leftover_prose(rendered):
    assert rendered["prose"] == ""


def test_markdown_inside_a_field_is_rendered_and_html_is_not(rendered):
    payload = recommendation_payload(
        {
            "headline": "x",
            "services": [
                {"name": "S3", "purpose": "`bucket`", "reasoning": "<img src=x onerror=alert(1)>"}
            ],
            "notes": [{"pillar": "Cost", "status": "review", "text": "**Watch** egress"}],
            "cost": {"tier": "Low", "detail": "under £50"},
        }
    )
    assert payload["services"][0]["purpose"] == "<code>bucket</code>"
    assert payload["services"][0]["reasoning"].startswith("&lt;img")
    assert payload["notes"][0]["text"] == "<strong>Watch</strong> egress"


# --------------------------------------------------------------------------- #
# Half-written and malformed replies
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "field",
    [
        "headline",
        "overview",
        "assumptions",
        "services",
        "notes",
        "cost",
        "next_questions",
        "diagram",
    ],
)
def test_a_missing_field_renders_as_nothing_rather_than_raising(field):
    partial = {key: value for key, value in RECOMMENDATION.items() if key != field}
    payload = recommendation_payload(partial)
    assert payload["structured"] is True


def test_an_empty_recommendation_renders_empty():
    payload = recommendation_payload({})
    assert payload["headline"] is None
    assert payload["services"] == []
    assert payload["notes"] == []
    assert payload["cost"] is None
    assert payload["nextQuestions"] == []
    assert payload["assumptions"] == []
    assert payload["coverage"] is None
    assert payload["diagram"] is None


@pytest.mark.parametrize(
    "junk",
    [
        {"services": "not a list", "notes": {"nope": 1}},
        {"services": [None, "text", {}], "notes": [None, {"text": ""}]},
        {"cost": "Medium", "diagram": 42, "headline": None},
    ],
)
def test_rubbish_in_a_field_is_dropped_not_rendered(junk):
    payload = recommendation_payload(junk)
    assert payload["services"] == []
    assert payload["notes"] == []


def test_a_prose_answer_is_rendered_as_markdown():
    diagram, prose = extract_mermaid(FOLLOW_UP)
    payload = prose_payload(prose, diagram)

    assert payload["structured"] is False
    assert payload["headline"] is None
    assert payload["services"] == []
    assert payload["cost"] is None
    assert payload["diagram"] is None
    assert "<ul><li>Move reporting queries" in payload["prose"]
    assert "<code>ReplicaLag</code>" in payload["prose"]
    assert "<strong>$220–320/month</strong>" in payload["prose"]


def test_a_follow_up_can_still_carry_a_revised_diagram():
    diagram, prose = extract_mermaid(
        'Try this instead.\n\n```mermaid\nflowchart LR\n  a["A"] --> b["B"]\n```\n'
    )
    payload = prose_payload(prose, diagram)
    assert payload["structured"] is False
    assert [node["id"] for node in payload["diagram"]["nodes"]] == ["a", "b"]


# --------------------------------------------------------------------------- #
# Reading a reply before it has finished arriving (R3)
# --------------------------------------------------------------------------- #


def is_part_of(part, whole) -> bool:
    """True if `part` is `whole`, or the start of it.

    A snapshot is allowed to be short -- three services out of seven -- but
    never wrong: no truncated word, no field belonging to something else.
    """
    if isinstance(part, dict) and isinstance(whole, dict):
        return all(key in whole and is_part_of(value, whole[key]) for key, value in part.items())
    if isinstance(part, list) and isinstance(whole, list):
        return len(part) <= len(whole) and all(
            is_part_of(item, whole[at]) for at, item in enumerate(part)
        )
    return part == whole


def test_a_reply_is_readable_at_every_length():
    """Every prefix of a real reply either reads cleanly or reads as nothing."""
    seen_fields: set[str] = set()
    for cut in range(1, len(FULL_JSON) + 1):
        data = partial_json(FULL_JSON[:cut])
        if data is None:
            continue
        assert isinstance(data, dict)
        assert is_part_of(data, RECOMMENDATION), f"cut at {cut} read something wrong"
        seen_fields.update(data)

    assert seen_fields == set(RECOMMENDATION)
    # The diagram is written last and is the one field with no complete value
    # until the document closes, so a half-drawn flowchart is never rendered.
    assert "diagram" not in (partial_json(FULL_JSON[:-1]) or {})


def test_fields_appear_in_the_order_they_are_written():
    first = next(
        partial_json(FULL_JSON[:cut])
        for cut in range(1, len(FULL_JSON))
        if partial_json(FULL_JSON[:cut])
    )
    assert list(first) == ["headline"]


def test_a_partial_reply_renders_what_has_arrived():
    counts = [
        len(recommendation_payload(partial_json(FULL_JSON[:cut]) or {})["services"])
        for cut in range(0, len(FULL_JSON) + 200, 200)
    ]
    assert counts[0] == 0
    assert counts[-1] == SERVICE_COUNT
    # The table fills in row by row rather than appearing all at once.
    assert any(0 < count < SERVICE_COUNT for count in counts)
    assert counts == sorted(counts)


def test_a_field_that_has_not_arrived_is_not_drawn():
    early = recommendation_payload(partial_json(FULL_JSON[:400]) or {})
    assert early["headline"]
    assert early["cost"] is None
    assert early["diagram"] is None


@pytest.mark.parametrize(
    "text",
    ["", "   ", "{", '{"a"', '{"a":', "not json at all", "}{", '{"a": 1}}'],
)
def test_nothing_readable_yet_is_not_an_error(text):
    assert partial_json(text) is None


def test_a_string_holding_a_comma_or_a_brace_does_not_confuse_the_cut():
    text = '{"detail": "£1,500 {peak}", "tier": "Med'
    assert partial_json(text) == {"detail": "£1,500 {peak}"}


def test_an_escaped_quote_inside_a_string_is_not_the_end_of_it():
    text = '{"label": "a \\" b", "next": "c'
    assert partial_json(text) == {"label": 'a " b'}


def test_a_finished_document_reads_whole():
    assert partial_json('{"a": 1, "b": [2, 3]}') == {"a": 1, "b": [2, 3]}
    assert partial_json(FULL_JSON) == json.loads(FULL_JSON)


# --------------------------------------------------------------------------- #
# The Markdown subset
# --------------------------------------------------------------------------- #


def test_headings_shift_down_one_level_and_stop_at_six():
    assert md_to_html("## Title") == "<h3>Title</h3>"
    assert md_to_html("###### Deep") == "<h6>Deep</h6>"


def test_a_paragraph_joins_its_lines():
    assert md_to_html("one\ntwo\n\nthree") == "<p>one two</p><p>three</p>"


def test_bullet_and_numbered_lists_do_not_merge():
    html = md_to_html("- a\n- b\n1. c\n2. d")
    assert html == "<ul><li>a</li><li>b</li></ul><ol><li>c</li><li>d</li></ol>"


def test_a_table_needs_a_divider_row():
    html = md_to_html("| A | B |\n|---|---|\n| 1 | 2 |")
    assert (
        html
        == "<table><thead><tr><th>A</th><th>B</th></tr></thead><tbody><tr><td>1</td><td>2</td></tr></tbody></table>"
    )
    # Without the divider it is just a paragraph, not a broken table.
    assert "<table>" not in md_to_html("| A | B |\n| 1 | 2 |")


def test_fenced_code_is_escaped_not_rendered():
    html = md_to_html("```\n<b>x</b>\n```")
    assert html == "<pre><code>&lt;b&gt;x&lt;/b&gt;</code></pre>"


def test_inline_marks():
    assert md_to_html("a **bold** and *thin* and `code`") == (
        "<p>a <strong>bold</strong> and <em>thin</em> and <code>code</code></p>"
    )


def test_html_in_a_reply_is_inert():
    assert md_to_html("<img src=x onerror=alert(1)>") == (
        "<p>&lt;img src=x onerror=alert(1)&gt;</p>"
    )


# --------------------------------------------------------------------------- #
# Link schemes (S1)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url",
    [
        "https://aws.amazon.com/s3/",
        "http://example.com",
        "HTTPS://example.com",
        "mailto:someone@example.com",
    ],
)
def test_safe_schemes_become_links(url):
    html = md_to_html(f"[docs]({url})")
    assert f'href="{url}"' in html
    assert 'rel="noopener noreferrer"' in html
    assert 'target="_blank"' in html


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "data:text/html;base64,PHNjcmlwdD4=",
        "vbscript:msgbox",
        "file:///etc/passwd",
        "//evil.example.com",
        "/relative/path",
    ],
)
def test_every_other_scheme_falls_back_to_plain_text(url):
    html = md_to_html(f"[click me]({url})")
    assert "<a " not in html
    assert "click me" in html
    assert url.split(":")[0] not in html


def test_a_dropped_link_keeps_only_its_label():
    assert md_to_html("[click me](vbscript:x)") == "<p>click me</p>"
    # A URL carrying its own bracket ends the match early, so the closing bracket
    # is left in the text. Harmless, and still not a link.
    assert md_to_html("[click me](javascript:alert(1))") == "<p>click me)</p>"


def test_a_query_string_stays_escaped():
    html = md_to_html("[docs](https://example.com/a?x=1&y=2)")
    assert 'href="https://example.com/a?x=1&amp;y=2"' in html


def test_the_sources_a_searched_answer_carries_render_as_a_list_of_links():
    """F2 writes cited pages as Markdown, so this path is what draws them."""
    reply = call_claude(
        FakeClient(
            fake_response(
                text="Ten per region.",
                citations=[cited("https://docs.aws.amazon.com/quotas", "Service quotas")],
            )
        ),
        [{"role": "user", "content": "how many?"}],
    )
    html = prose_payload(reply.text)["prose"]

    assert "<h5>Sources</h5>" in html
    assert '<ol><li><a href="https://docs.aws.amazon.com/quotas"' in html
    assert "Ten per region. [1]" in html


def test_a_link_in_a_recommendation_field_is_checked_too():
    payload = recommendation_payload(
        {"notes": [{"pillar": "Cost", "status": "good", "text": "[see](javascript:alert(1))"}]}
    )
    assert "<a " not in payload["notes"][0]["text"]


# --------------------------------------------------------------------------- #
# Diagram parsing
# --------------------------------------------------------------------------- #


def test_a_chain_yields_every_link():
    diagram = parse_diagram('flowchart LR\n  a["A"] --> b["B"] --> c["C"]')
    assert [(edge["from"], edge["to"]) for edge in diagram["edges"]] == [("a", "b"), ("b", "c")]
    assert [node["depth"] for node in diagram["nodes"]] == [0, 1, 2]


def test_edge_labels_and_declarations_are_not_nodes():
    diagram = parse_diagram('flowchart LR\n  a["A"] -->|writes| b["B"]\n  %% a comment\n  a --> b')
    assert [node["id"] for node in diagram["nodes"]] == ["a", "b"]
    assert diagram["nodes"][0]["label"] == "A"
    # It is not a node, and it is not thrown away any more either (F7).
    assert diagram["edges"][0]["label"] == "writes"


def test_depth_takes_the_longest_path():
    diagram = parse_diagram('flowchart LR\n  a["A"] --> b["B"]\n  a --> c["C"]\n  b --> c')
    depths = {node["id"]: node["depth"] for node in diagram["nodes"]}
    assert depths == {"a": 0, "b": 1, "c": 2}


def test_a_label_declared_once_is_reused():
    diagram = parse_diagram('flowchart LR\n  a["Users"] --> b["ALB"]\n  b --> a')
    labels = {node["id"]: node["label"] for node in diagram["nodes"]}
    assert labels == {"a": "Users", "b": "ALB"}


def test_an_unlabelled_node_falls_back_to_its_id():
    diagram = parse_diagram("flowchart LR\n  alb --> ecs")
    assert [node["label"] for node in diagram["nodes"]] == ["alb", "ecs"]


def test_a_chain_labels_every_link_it_names():
    """The label sits between the arrow and its target, so it has to be held."""
    diagram = parse_diagram("flowchart LR\n  a -->|reads| b -->|writes to| c\n  a --> c")
    assert [edge["label"] for edge in diagram["edges"]] == ["reads", "writes to", ""]


def test_an_edge_label_is_stripped_to_what_is_safe_to_draw():
    """It reaches the SVG as text, so the characters that could break it go."""
    diagram = parse_diagram("flowchart LR\n  a -->|<script>x</script>| b")
    label = diagram["edges"][0]["label"]
    assert "<" not in label and ">" not in label
    assert "script" in label


def test_a_long_edge_label_is_truncated_rather_than_drawn_over_a_node():
    diagram = parse_diagram("flowchart LR\n  a -->|writes every single row of it| b")
    label = diagram["edges"][0]["label"]
    assert len(label) == MAX_EDGE_LABEL
    assert label.endswith("\u2026")


def test_a_boundary_holds_the_nodes_declared_inside_it():
    """F7: `subgraph` is read now rather than skipped."""
    diagram = parse_diagram(
        'flowchart LR\n  subgraph vpc["Production VPC"]\n    a["A"] --> b["B"]\n  end\n'
    )
    assert [(g["id"], g["label"], g["level"], g["parent"]) for g in diagram["groups"]] == [
        ("vpc", "Production VPC", 0, None)
    ]
    assert diagram["groups"][0]["members"] == ["a", "b"]


def test_an_availability_zone_nests_inside_a_vpc():
    diagram = parse_diagram(
        "flowchart LR\n"
        '  subgraph vpc["VPC"]\n'
        '    subgraph az["eu-west-2a"]\n'
        '      a["A"] --> b["B"]\n'
        "    end\n"
        "  end\n"
    )
    zone = next(group for group in diagram["groups"] if group["id"] == "az")
    assert zone["level"] == 1
    assert zone["parent"] == "vpc"
    # The VPC holds nothing directly and is still drawn: what is in it is in its
    # zones, and a box round those is the boundary the reader wants.
    assert next(g for g in diagram["groups"] if g["id"] == "vpc")["members"] == []


def test_a_node_declared_outside_a_boundary_stays_outside_it():
    """Filed where it is first seen. A later bare mention must not move it."""
    diagram = parse_diagram(
        "flowchart LR\n"
        '  users["Users"] --> alb["ALB"]\n'
        '  subgraph vpc["VPC"]\n'
        '    alb --> asg["ASG"]\n'
        '    asg --> rds["RDS"]\n'
        "  end\n"
    )
    assert diagram["groups"][0]["members"] == ["asg", "rds"]


def test_a_boundary_nested_too_deep_gives_its_nodes_to_its_parent():
    """Losing the box is a smaller loss than losing what was inside it."""
    diagram = parse_diagram(
        "flowchart LR\n"
        '  subgraph a["A"]\n'
        '    subgraph b["B"]\n'
        '      subgraph c["C"]\n'
        '        x["X"] --> y["Y"]\n'
        "      end\n"
        "    end\n"
        "  end\n"
    )
    assert [group["id"] for group in diagram["groups"]] == ["a", "b"]
    assert next(g for g in diagram["groups"] if g["id"] == "b")["members"] == ["x", "y"]


def test_an_unclosed_boundary_closes_at_the_end_of_the_source():
    diagram = parse_diagram('flowchart LR\n  subgraph vpc["VPC"]\n    a["A"] --> b["B"]\n')
    assert diagram["groups"][0]["members"] == ["a", "b"]
    assert [node["id"] for node in diagram["nodes"]] == ["a", "b"]


def test_a_stray_end_is_ignored_rather_than_refused():
    diagram = parse_diagram('flowchart LR\n  a["A"] --> b["B"]\n  end\n')
    assert diagram["groups"] == []
    assert [node["id"] for node in diagram["nodes"]] == ["a", "b"]


def test_a_boundary_round_one_node_is_not_drawn():
    """A box round a single node says nothing its own outline does not."""
    diagram = parse_diagram(
        'flowchart LR\n  subgraph vpc["VPC"]\n    a["A"]\n  end\n  a --> b["B"]\n'
    )
    assert diagram["groups"] == []


def test_a_dropped_boundary_hands_its_nodes_to_the_one_above_it():
    """A box may go; the nodes in it may not fall out of the box around it.

    Three levels deep, so the innermost goes -- and its node has to end up in the
    survivor's members, or the box drawn for that survivor is computed without it
    and the node is drawn outside the boundary it belongs to.
    """
    diagram = parse_diagram(
        "flowchart LR\n"
        '  subgraph vpc["VPC"]\n'
        '    subgraph az["AZ A"]\n'
        '      subgraph rack["Rack 1"]\n'
        '        a["A"] --> b["B"]\n'
        "      end\n"
        "    end\n"
        "  end\n"
    )
    zone = next(group for group in diagram["groups"] if group["id"] == "az")
    assert zone["members"] == ["a", "b"]


def test_more_boundaries_than_can_be_drawn_keeps_the_first_of_them():
    source = ["flowchart LR"]
    for index in range(8):
        source.append(f'  subgraph g{index}["G{index}"]')
        source.append(f'    a{index}["A{index}"] --> b{index}["B{index}"]')
        source.append("  end")
    diagram = parse_diagram("\n".join(source))
    assert [group["id"] for group in diagram["groups"]] == [f"g{index}" for index in range(6)]


def test_a_boundary_label_is_capped():
    long_name = "V" * 60
    diagram = parse_diagram(
        f'flowchart LR\n  subgraph vpc["{long_name}"]\n    a["A"] --> b["B"]\n  end\n'
    )
    assert len(diagram["groups"][0]["label"]) == MAX_GROUP_LABEL


def test_the_sample_carries_the_boundaries_a_real_one_has(rendered):
    """The shared sample exercises this, so every suite does (F7)."""
    groups = rendered["diagram"]["groups"]
    assert len(groups) == GROUP_COUNT
    assert [group["level"] for group in groups] == [0, 1, 1]


def test_source_is_kept_even_when_nothing_parses():
    diagram = parse_diagram("flowchart LR\n")
    assert diagram == {
        "source": "flowchart LR\n",
        "nodes": [],
        "edges": [],
        "groups": [],
    }


def test_no_diagram_at_all():
    assert parse_diagram("") is None
    assert recommendation_payload({"diagram": ""})["diagram"] is None
