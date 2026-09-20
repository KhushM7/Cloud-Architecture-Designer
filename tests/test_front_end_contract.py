"""What static/app.js has to keep in step with Python, and with itself.

The front end has no build step, which is a property this project values and
pays for in duplication: the regions, the compliance regimes, the cost bands and
the compare bounds all exist twice, once in Python and once as a literal in
app.js. Two of those pairs had nothing checking them, so adding a region to
schema.Region left the selector quietly not offering it.

This is the same trick tests/test_export.py plays on the layout constants: read
the JavaScript as text, pull the literals out, and compare them to the Python
that is the authority. (The palette used to be checked this way too; it is now
generated from branding.py instead, and tests/test_design_tokens.py asserts that
no copy of it has crept back in.) It is not a substitute for the browser suite -- it says the two lists
agree, not that the app works -- and it costs nothing to run.

The second half is about app.js agreeing with itself: the delegated click handler
derives its selector from its own handler map now, and this checks that every
`data-` attribute the app actually emits is answered by something.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import server
from schema import Compliance, Region, Tier

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def js_array(name: str) -> str:
    """The body of a `const NAME = [...]` declaration in app.js."""
    found = re.search(rf"const {name} = \[(.*?)\];", APP_JS, re.DOTALL)
    assert found, f"const {name} = [...] is not in app.js"
    return found.group(1)


def js_ids(name: str) -> list[str]:
    """The `id: '...'` values of an array of {id, label} objects."""
    return re.findall(r"id: '([^']*)'", js_array(name))


# --------------------------------------------------------------------------- #
# The enums, which live in schema.py and are copied into app.js (Q2)
# --------------------------------------------------------------------------- #


def test_the_regions_offered_are_the_regions_that_can_be_priced():
    """schema.Region is the authority; the selector is a copy of it.

    The empty first entry is not a region -- it is "let the advisor decide",
    which is why the list is not simply the enum.
    """
    offered = js_ids("REGIONS")

    assert offered[0] == "", "the first entry should be the advisor's own choice"
    assert offered[1:] == [region.value for region in Region]


def test_the_compliance_regimes_offered_are_the_ones_the_model_is_told_about():
    """Compliance carries NONE itself, so this one compares one to one."""
    assert js_ids("COMPLIANCE") == [item.value for item in Compliance]


def test_the_cost_bands_offered_as_filters_are_the_bands_a_reply_can_carry():
    """Including the en dashes, which are load-bearing: Tier is a StrEnum.

    A hyphen here would be a filter chip that matches nothing, because nothing is
    ever stored under "Low-Medium".
    """
    bands = re.findall(r"'([^']*)'", js_array("TIERS"))

    assert bands == [tier.value for tier in Tier]
    assert "Low–Medium" in bands


def test_a_comparison_is_the_same_width_on_both_sides():
    """server.py is the authority and the CLI already matches it (F9)."""
    assert f"const MIN_COMPARE = {server.MIN_COMPARE};" in APP_JS
    assert f"const MAX_COMPARE = {server.MAX_COMPARE};" in APP_JS


def test_the_model_the_page_falls_back_to_is_the_one_the_server_uses():
    """The label is replaced by /api/health, but the first paint uses this."""
    assert f"const DEFAULT_MODEL = '{server.MODEL}';" in APP_JS


# --------------------------------------------------------------------------- #
# The delegated click handler, which derives its own selector (Q5)
# --------------------------------------------------------------------------- #

# Attributes another listener owns, so the click handler is right not to have
# them. The backdrops are deliberate: they are answered from event.target
# before closest() widens the search, because hitting exactly that element and
# not something inside it is the gesture.
ELSEWHERE = {
    "data-discard-backdrop",
    "data-export-backdrop",
    "data-usage-backdrop",
    # The `change` listener.
    "data-constraint",
    "data-export-format",
    "data-export-toggle",
    # The `keydown` and capture-phase `blur` listeners.
    "data-rename-input",
    "data-assumption-input",
    "data-tag-input",
    # Read as the payload of another attribute rather than as a trigger.
    "data-tag-name",
    # A marker paintEstimate reads off the slot it is repainting.
    "data-compact",
}


def handler_keys() -> list[str]:
    """The keys of the CLICK_HANDLERS map, in the order they are declared."""
    start = APP_JS.index("const CLICK_HANDLERS = {")
    end = APP_JS.index("const CLICK_IDS", start)
    return re.findall(r"^  ([A-Za-z]+):", APP_JS[start:end], re.MULTILINE)


def as_attribute(key: str) -> str:
    """`rebuildAssumptions` -> `data-rebuild-assumptions`, as the DOM maps it."""
    return "data-" + re.sub(r"([A-Z])", lambda m: "-" + m.group(1).lower(), key)


# Attributes whose name is composed at run time, so no literal to find: the
# filter chips write `data-filter-${field}` once for each facet they offer.
# test_the_filter_chips_compose_their_own_attribute_name keeps this honest.
DYNAMIC = {"data-filter-tag", "data-filter-tier"}


def emitted_attributes() -> set[str]:
    """Every `data-` attribute the app writes into the DOM."""
    literal = {f"data-{name}" for name in re.findall(r"data-([a-z-]+)=", APP_JS + INDEX_HTML)}
    return literal | DYNAMIC


def test_the_handler_map_is_not_empty():
    """A regex that stopped matching would make every test below vacuous."""
    assert len(handler_keys()) > 20


def test_every_attribute_the_app_emits_is_answered_by_something():
    """Q5. The bug this closes: an attribute added to one list and not the other.

    The selector used to be a hand-written string beside a hand-written chain of
    `if (data.x)` branches. Forgetting the selector gave a button that rendered,
    looked live, and did nothing at all -- silently, because nothing matched it.
    """
    handled = {as_attribute(key) for key in handler_keys()}

    for attribute in sorted(emitted_attributes()):
        assert attribute in handled or attribute in ELSEWHERE, attribute


def test_nothing_in_the_handler_map_is_dead():
    """The other direction: a handler for something the app never renders."""
    emitted = emitted_attributes()

    for key in handler_keys():
        assert as_attribute(key) in emitted, key


def test_the_filter_chips_compose_their_own_attribute_name():
    """Which is why DYNAMIC exists, and why it is two names rather than a guess."""
    assert "data-filter-${field}=" in APP_JS
    for field in ("tag", "tier"):
        assert f"'{field}'" in APP_JS


def test_the_selector_is_derived_rather_than_written_out():
    """If it goes back to being a literal, these tests stop meaning anything."""
    assert "const CLICK_TARGETS = CLICK_KEYS.map(" in APP_JS
    # The old form, kept out by name.
    assert "'[data-example], [data-suggestion]" not in APP_JS


@pytest.mark.parametrize(
    ("key", "attribute"),
    [
        ("rebuildAssumptions", "data-rebuild-assumptions"),
        ("downloadSvg", "data-download-svg"),
        ("filterTier", "data-filter-tier"),
        ("deleteYes", "data-delete-yes"),
        ("untag", "data-untag"),
    ],
)
def test_a_camel_case_key_becomes_the_attribute_the_dom_uses(key, attribute):
    """The derivation itself, on the shapes that actually occur."""
    assert as_attribute(key) == attribute


# --------------------------------------------------------------------------- #
# A failed estimate can be asked for again (C4)
# --------------------------------------------------------------------------- #


def test_a_failed_estimate_offers_to_try_again():
    """C4. requestEstimate returns early once an entry exists, error included.

    So one unreachable price list left an architecture unpriced for the life of
    the session, and the autosave restored that state rather than clearing it.
    Both the full element and the compact one in a comparison column offer it.
    """
    assert 'data-reprice="${esc(id)}"' in APP_JS
    assert APP_JS.count('data-reprice="${esc(id)}"') == 2
    assert "async function repriceEstimate(id)" in APP_JS
    # It has to clear the held entry, or requestEstimate returns early again.
    assert "delete state.estimates[id];" in APP_JS


def test_the_raw_reply_is_kept_where_a_retry_can_find_it():
    """And not in `state`, which snapshot() writes to localStorage."""
    assert "const estimateSources = new Map();" in APP_JS
    assert "estimateSources" not in APP_JS[APP_JS.index("function snapshot()") :][:600]
