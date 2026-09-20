"""The front end, in a real browser.

Opt-in: these need Chrome (or Edge) and a free port, so they are marked `slow` and
left out of the default run. `pytest -m slow` runs them alone, `pytest -m ""` runs
everything. The Claude API is stubbed exactly as in the other suites, so nothing
here costs money.

What is worth testing here is only what the browser does with a reply: the JSON is
already covered by test_server.py.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

import advisor
import server
import store
from tests.browser import Browser, find_chrome
from tests.conftest import ApiStub
from tests.samples import (
    ASSUMPTIONS,
    FULL_JSON,
    GROUP_COUNT,
    NEXT_QUESTIONS,
    NODE_COUNT,
    SERVICE_COUNT,
)

pytestmark = pytest.mark.slow

WORKLOAD = "An e-commerce site expecting a 10x spike over Black Friday"

# Every reply streams, so a test that looks at one has to wait for it to finish
# arriving rather than for the first thing to appear on screen. render() keeps
# `is-busy` on <body> for exactly as long as a request is in flight.
SETTLED = "!document.body.classList.contains('is-busy')"


def finished(n: int) -> str:
    """A wait for n finished replies, not counting the one still arriving."""
    return f"document.querySelectorAll('.msg .reply:not(#live-reply)').length === {n}"


@pytest.fixture(scope="module")
def live() -> Iterator[SimpleNamespace]:
    """The real app on a real port, with the API stubbed and a scratch store."""
    pytest.importorskip("websockets", reason="the browser driver needs websockets")
    from werkzeug.serving import make_server

    stub = ApiStub()
    scratch = Path(tempfile.mkdtemp(prefix="advisor-ui-"))

    # A module-scoped fixture cannot use the function-scoped monkeypatch fixture,
    # so it opens its own context and lets that put everything back.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(server, "call_claude", stub.call_claude)
        patch.setattr(server, "advise", stub.advise)
        patch.setattr(server, "get_client", lambda: object())
        patch.setattr(store, "DB_PATH", scratch / "advisor.db")
        store.reset_for_tests()
        # The suite asks far more questions in a minute than a person would, so
        # the spend guards are off here. test_server.py is where they are tested.
        patch.setattr(server.limiter, "enabled", False)

        httpd = make_server("127.0.0.1", 0, server.app, threaded=True)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield SimpleNamespace(url=f"http://127.0.0.1:{httpd.server_port}", stub=stub)
        finally:
            httpd.shutdown()
            thread.join(timeout=5)
            store.reset_for_tests()
            shutil.rmtree(scratch, ignore_errors=True)


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    binary = find_chrome()
    if not binary:
        pytest.skip("no Chrome or Edge found; set CHROME_PATH to run the browser suite")

    profile = Path(tempfile.mkdtemp(prefix="advisor-chrome-"))
    driver = Browser(binary, profile)
    try:
        yield driver
    finally:
        driver.close()
        shutil.rmtree(profile, ignore_errors=True)


@pytest.fixture
def page(browser: Browser, live: SimpleNamespace) -> Browser:
    """A freshly loaded app for each test, so nothing leaks between them."""
    live.stub.calls.clear()
    live.stub.raise_with = None
    live.stub.delay = 0.0
    for item in store.listing():
        store.delete(item["id"])

    # A3 keeps a live conversation in localStorage, which is the point of it and
    # would otherwise leak one test's thread into the next. Clearing the store is
    # not enough on its own: the app flushes what it is holding on the way out,
    # so what it is holding has to go as well.
    browser.goto(live.url, ready=False)
    browser.evaluate(
        "state.sessions = []; state.compare.results = null; state.compare.usage = null;"
        " state.tab = 'advisor'; localStorage.clear(); true"
    )
    browser.goto(live.url)
    return browser


# --------------------------------------------------------------------------- #
# The first screen
# --------------------------------------------------------------------------- #


def test_the_app_loads_with_its_welcome_and_examples(page):
    assert "Architecture Advisor" in page.evaluate("document.title")
    assert page.count(".example") == 3
    assert page.exists("#composer-input")
    assert page.exists("#send-btn")
    # Nothing has been asked yet, so there is no session row and nothing to save.
    assert page.count("#sessions .session") == 0
    assert page.evaluate("document.getElementById('save-btn').disabled") is True
    assert "Nothing saved yet" in page.text("#saved-list")


def test_the_model_comes_from_the_health_check(page):
    page.wait_for("document.getElementById('model-label').textContent.length > 0")
    assert page.text("#model-label") == advisor.MODEL


def test_nothing_on_the_page_says_who_is_running_it(page):
    """S5: the sidebar avatar used to show the machine account's initial."""
    import getpass

    assert not page.exists("#user-avatar")
    assert getpass.getuser().lower() not in page.evaluate("document.body.innerText").lower()


# --------------------------------------------------------------------------- #
# Asking a question
# --------------------------------------------------------------------------- #


def test_a_recommendation_renders_as_components_not_markdown(page, live):
    page.click(".example")
    page.wait_for(SETTLED)

    assert "Spread the load" in page.text(".rec__title")
    assert page.count(".services__body") == SERVICE_COUNT
    assert page.count(".note") == 5
    # Three ticks and two warnings, and only the warnings are marked for review.
    assert page.count(".note--review") == 2
    # F12: one cost element, not an orange tier panel above a priced card.
    assert page.count(".cost-panel") == 0
    assert page.count(".cost-inline") == 0
    assert page.count(".estimate") == 1
    # The Mermaid source is laid out as SVG rather than shown as code.
    assert page.count(".diagram svg rect") == NODE_COUNT + GROUP_COUNT
    assert page.count(".diagram svg path[marker-end]") == 7
    assert live.stub.count == 1


def test_the_running_cost_sits_on_the_session_it_belongs_to(page):
    """Per conversation, on its own row. The single cross-session total is gone."""
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    page.wait_for("!!document.querySelector('#sessions .session__cost')")

    first = page.text("#sessions .session__cost")
    assert first.startswith("$")
    # The breakdown is the row's tooltip rather than a panel of its own.
    detail = page.evaluate(
        "document.querySelector('#sessions .session__cost').getAttribute('title')"
    )
    assert "1 API call" in detail
    assert "in / " in detail
    assert "list price, USD" in detail

    page.ask("How would I cut the cost?")
    page.wait_for(finished(2))

    grown = page.evaluate(
        "document.querySelector('#sessions .session__cost').getAttribute('title')"
    )
    assert "2 API calls" in grown
    assert page.text("#sessions .session__cost") != first


def test_a_follow_up_is_prose_and_offers_suggestions(page):
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    page.ask("Is the read replica worth it?")
    page.wait_for(finished(2))

    replies = page.evaluate("document.querySelectorAll('.msg .reply')[1].innerHTML")
    assert "<ul>" in replies
    assert "<code>ReplicaLag</code>" in replies
    assert page.count(".suggestion") == len(NEXT_QUESTIONS)
    # The second question means there is context worth summarising at the top.
    assert page.exists(".context")


def test_the_diagram_source_can_be_shown_and_hidden(page):
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    assert not page.exists(".source")

    page.click(".diagram__toggle")
    page.wait_for("!!document.querySelector('.source pre')")
    assert "flowchart LR" in page.text(".source pre")

    page.click(".diagram__toggle")
    page.wait_for("!document.querySelector('.source')")


def test_an_empty_question_is_not_sent(page, live):
    page.click("#send-btn")
    assert live.stub.count == 0
    assert not page.exists(".msg")


# --------------------------------------------------------------------------- #
# Watching a reply arrive (R3)
# --------------------------------------------------------------------------- #


def test_the_reasoning_shows_while_there_is_nothing_else_to_show(page, live):
    """The wait used to be a static "Thinking..." with nothing behind it."""
    live.stub.delay = 0.4
    page.ask(WORKLOAD)

    page.wait_for("!!document.querySelector('#live-reply .thinking__note')")
    assert page.text("#live-reply .thinking__note")
    # Nothing has been committed to the thread yet.
    assert page.count(".msg .reply:not(#live-reply)") == 0

    page.wait_for(SETTLED, timeout=20)
    assert not page.exists("#live-reply")


def test_a_recommendation_fills_in_while_it_is_written(page, live):
    live.stub.delay = 0.4
    page.ask(WORKLOAD)

    # The headline lands first, well before the services table is complete.
    page.wait_for("!!document.querySelector('#live-reply .rec__title')")
    assert "Spread the load" in page.text("#live-reply .rec__title")
    assert page.count("#live-reply .services__body") < SERVICE_COUNT

    page.wait_for(SETTLED, timeout=20)
    assert page.count(".services__body") == SERVICE_COUNT


def test_both_comparison_columns_fill_in_at_once(page, live):
    live.stub.delay = 0.4
    page.click("#tab-compare")
    page.wait_for("!!document.querySelector('#workload-0')")
    page.fill("#workload-0", "Serverless pipeline")
    page.fill("#workload-1", "The same pipeline on EC2")
    page.click("#compare-btn")

    page.wait_for("document.querySelectorAll('.option').length === 2")
    assert page.count(".option .thinking") >= 1

    page.wait_for(SETTLED, timeout=20)
    assert page.count(".option .thinking") == 0
    assert page.count(".option:nth-of-type(2) .stack__row") == SERVICE_COUNT


# --------------------------------------------------------------------------- #
# Updating in place rather than rebuilding (A2)
# --------------------------------------------------------------------------- #

# Marking a node and checking the mark is still there afterwards is the only
# honest way to tell "updated in place" from "rebuilt to look the same".
MARK = "document.querySelectorAll('.msg')[0].dataset.marker = 'kept'"
MARKED = "document.querySelectorAll('.msg')[0].dataset.marker === 'kept'"


def test_asking_again_leaves_the_thread_above_it_alone(page):
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    page.evaluate(MARK)

    page.ask("Is the read replica worth it?")
    page.wait_for(finished(2))

    assert page.evaluate(MARKED) is True
    assert page.count(".msg") == 4  # two questions, two replies


def test_the_reply_that_was_streaming_is_not_rebuilt_when_it_finishes(page, live):
    live.stub.delay = 0.3
    page.ask(WORKLOAD)
    page.wait_for("!!document.querySelector('#live-reply .rec')")
    page.evaluate("document.querySelector('#live-reply').dataset.marker = 'kept'")

    page.wait_for(SETTLED, timeout=20)
    # The same node became the finished reply rather than being replaced.
    assert page.evaluate("document.querySelector('.reply').dataset.marker === 'kept'") is True
    assert not page.exists("#live-reply")


def test_showing_the_diagram_source_does_not_rebuild_the_thread(page):
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    page.evaluate(MARK)

    page.click(".diagram__toggle")
    page.wait_for("!!document.querySelector('.source pre')")
    assert "flowchart LR" in page.text(".source pre")
    assert page.evaluate(MARKED) is True

    page.click(".diagram__toggle")
    page.wait_for("!document.querySelector('.source')")
    assert page.evaluate(MARKED) is True


# --------------------------------------------------------------------------- #
# Surviving a reload (A3)
# --------------------------------------------------------------------------- #


def test_a_conversation_is_still_there_after_a_reload(page, live):
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    page.ask("Is the read replica worth it?")
    page.wait_for(finished(2))
    spent = page.text("#sessions .session__cost")

    page.goto(live.url)
    page.wait_for(finished(2))

    assert page.count(".bubble") == 2
    assert "Spread the load" in page.text(".rec__title")
    assert page.count(".services__body") == SERVICE_COUNT
    # And what it cost came back with it.
    assert page.text("#sessions .session__cost") == spent
    # Nothing was saved to the server: this is the browser's own copy.
    assert store.listing() == []


def test_a_question_interrupted_by_a_reload_comes_back_in_the_composer(page, live):
    """The answer never arrived, so the thread must not pretend it did."""
    live.stub.delay = 0.6
    page.ask("A question that will not finish in time")
    page.wait_for("!!document.querySelector('#live-reply')")

    page.goto(live.url)
    page.wait_for("!!document.querySelector('#composer-input')")

    assert page.evaluate("document.getElementById('composer-input').value") == (
        "A question that will not finish in time"
    )
    assert page.count(".bubble") == 0


def test_a_new_conversation_stays_new_across_a_reload(page, live):
    """Clearing and reloading must not put the old conversation back."""
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)

    # Unsaved, so clearing asks first.
    page.click("#clear-btn")
    page.wait_for("!!document.querySelector('[data-discard-anyway]')")
    page.click("[data-discard-anyway]")
    page.wait_for("!document.querySelector('.msg')")

    page.goto(live.url)
    assert page.count(".msg") == 0
    assert page.exists(".welcome")
    # Lost, as the warning said it would be: no row left behind for it.
    assert page.count("#sessions .session") == 0


# --------------------------------------------------------------------------- #
# Saved conversations
# --------------------------------------------------------------------------- #


def test_a_conversation_can_be_saved_renamed_and_deleted(page, live):
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)

    page.click("#save-btn")
    page.wait_for("!!document.querySelector('#saved-list .session')")
    assert len(store.listing()) == 1
    assert page.evaluate("document.getElementById('saved').hidden") is False
    # A5: the name came back from the server rather than being worked out here.
    assert page.text("#saved-path") == f"Saved as “{store.listing()[0]['title']}”"

    page.click("#saved-list [data-rename]")
    page.wait_for("!!document.querySelector('[data-rename-input]')")
    page.fill("[data-rename-input]", "Black Friday plan")
    page.evaluate(
        "document.querySelector('[data-rename-input]')"
        ".dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}))"
    )
    page.wait_for(
        "document.querySelector('#saved-list .session__title')"
        ".textContent.includes('Black Friday plan')"
    )

    page.click("#saved-list [data-delete]")
    page.wait_for("!!document.querySelector('#saved-list .confirm')")
    assert "Delete" in page.text(".confirm__text")

    # The confirm row replaces the item, so wait for the empty state rather than
    # for the item to go: the latter is already true while the confirm is up.
    page.click("[data-delete-yes]")
    page.wait_for("document.getElementById('saved-list').textContent.includes('Nothing saved yet')")
    assert store.listing() == []


def test_reopening_a_saved_conversation_restores_its_cost(page, live):
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    page.click("#save-btn")
    page.wait_for("!!document.querySelector('#saved-list .session')")
    spent = page.text("#sessions .session__cost")

    # A3 means a reload keeps the live conversation, so start a fresh one to
    # prove the cost being shown afterwards came from the file. Saved, so
    # clearing does not stop to ask.
    page.goto(live.url)
    page.click("#clear-btn")
    page.wait_for("!document.querySelector('#sessions .session')")
    page.wait_for("!!document.querySelector('#saved-list .session')")

    page.click("#saved-list [data-open]")
    page.wait_for("!!document.querySelector('.rec')")
    assert page.text("#saved-list .session__cost") == spent


# --------------------------------------------------------------------------- #
# Comparing
# --------------------------------------------------------------------------- #


def test_a_saved_conversation_can_be_downloaded_as_json(page, live):
    """A1: the store is SQLite; the portable format is still one click away."""
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    page.click("#save-btn")
    page.wait_for("!!document.querySelector('#saved-list .session')")

    href = page.evaluate(
        "document.querySelector('#saved-list a[href*=export]').getAttribute('href')"
    )
    assert href == f"/api/conversations/{store.listing()[0]['id']}/export"


def test_two_workloads_render_side_by_side(page, live):
    page.click("#tab-compare")
    page.wait_for("!!document.querySelector('#workload-0')")

    page.fill("#workload-0", "Serverless pipeline")
    page.fill("#workload-1", "The same pipeline on EC2")
    page.click("#compare-btn")
    page.wait_for(SETTLED)

    badges = page.evaluate(
        "Array.from(document.querySelectorAll('.option__badge')).map((n) => n.textContent)"
    )
    assert badges == ["Option A", "Option B"]
    assert page.count(".option:nth-of-type(1) .stack__row") == SERVICE_COUNT
    assert live.stub.count == 2


def test_comparing_needs_both_sides(page, live):
    page.click("#tab-compare")
    page.wait_for("!!document.querySelector('#compare-btn')")
    page.fill("#workload-0", "Only one side")
    page.click("#compare-btn")
    page.wait_for("!!document.querySelector('.alert')")

    assert "Nothing to compare yet" in page.text(".alert__title")
    assert live.stub.count == 0


# --------------------------------------------------------------------------- #
# Failures
# --------------------------------------------------------------------------- #


def test_a_failure_is_explained_and_can_be_retried(page, live):
    live.stub.raise_with = advisor.AdvisorError(
        "Claude ran out of room before finishing", kind="truncated"
    )
    page.ask(WORKLOAD)
    page.wait_for("!!document.querySelector('.alert')")

    assert "cut short" in page.text(".alert__title")
    assert "ran out of room" in page.text(".alert__text")
    assert page.exists("[data-action='retry']")

    live.stub.raise_with = None
    page.click("[data-action='retry']")
    page.wait_for(SETTLED)
    # The retry re-sent the question rather than duplicating it.
    assert page.count(".bubble") == 1


def test_a_truncated_reply_still_shows_what_it_cost(page, live):
    live.stub.raise_with = advisor.AdvisorError(
        "cut short",
        kind="truncated",
        usage=advisor.merge_usage({"calls": 1, "inputTokens": 700, "costUsd": 0.01}),
    )
    page.ask(WORKLOAD)
    page.wait_for("!!document.querySelector('#sessions .session__cost')")

    assert page.text("#sessions .session__cost") == "$0.0100"
    detail = page.evaluate(
        "document.querySelector('#sessions .session__cost').getAttribute('title')"
    )
    assert "1 API call" in detail


def test_a_missing_key_blocks_the_composer(page, live, monkeypatch):
    """The health check runs on load, so the app knows before you type."""

    def refuse():
        raise advisor.AdvisorError("ANTHROPIC_API_KEY is missing.", kind="missing_key")

    monkeypatch.setattr(server, "get_client", refuse)
    page.goto(live.url)
    page.wait_for("!!document.querySelector('.alert--danger')")
    assert "API key missing" in page.text(".alert__title")
    assert page.evaluate("document.getElementById('composer-input').disabled") is True

    page.click("[data-action='guide']")
    page.wait_for("!!document.querySelector('.setup')")
    assert "Setup" in page.text(".setup")


# --------------------------------------------------------------------------- #
# Rendering safety, end to end (S1)
# --------------------------------------------------------------------------- #


def test_an_unsafe_link_in_a_reply_never_becomes_a_link(page, live):
    live.stub.later = "See [the docs](javascript:alert(document.domain)) for more."
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    page.ask("Where are the docs?")
    page.wait_for(finished(2))

    # Not "no anchors at all": since F6 every pillar badge is one. What must not
    # exist is an anchor the model supplied, and `javascript:` is the case.
    hrefs = page.evaluate(
        "Array.from(document.querySelectorAll('.reply a')).map((n) => n.getAttribute('href'))"
    )
    assert all(href.startswith("https://docs.aws.amazon.com/") for href in hrefs)
    assert not any("javascript" in href.lower() for href in hrefs)
    assert "the docs" in page.text(".msg:last-child .reply")


def test_a_safe_link_opens_in_a_new_tab(page, live):
    live.stub.later = "See [the docs](https://docs.aws.amazon.com/) for more."
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    page.ask("Where are the docs?")
    page.wait_for("!!document.querySelector('.reply a')")

    assert page.evaluate("document.querySelector('.reply a').target") == "_blank"
    assert page.evaluate("document.querySelector('.reply a').rel") == "noopener noreferrer"


# --------------------------------------------------------------------------- #
# The export dialog (F3)
#
# What is worth testing in a browser is what only a browser does: the dialog
# opening over the app, the filename following what is typed, and a real file
# arriving in a real Downloads folder. The documents themselves are
# test_export.py's job.
# --------------------------------------------------------------------------- #


def open_export(page) -> None:
    page.click("#export-btn")
    page.wait_for("!!document.querySelector('.dialog')")


def test_there_is_nothing_to_export_from_an_empty_session(page):
    assert page.evaluate("document.getElementById('export-btn').disabled") is True


def test_a_reply_makes_the_export_button_live(page):
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    assert page.evaluate("document.getElementById('export-btn').disabled") is False


def test_the_dialog_offers_every_format_with_three_chosen(page):
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    open_export(page)

    # Five formats, and the two nobody should get without asking -- the raw
    # session and the Terraform module -- start off.
    assert page.count(".format") == 5
    assert page.count(".format.is-on") == 3
    assert "Export 3 files" in page.text("[data-export-go]")
    assert "session.json" in page.text(".dialog__body")
    assert "terraform/" in page.text(".dialog__body")


def test_choosing_the_terraform_module_warns_before_it_is_written(page):
    """F4 asked for it to be labelled in the UI. This is the label."""
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    open_export(page)

    assert page.count(".dialog__note") == 0

    page.click("[data-export-format='tf']")
    page.wait_for("document.querySelectorAll('.format.is-on').length === 4")

    warning = page.text(".dialog__note")
    assert "has not been applied to any account" in warning
    assert "terraform plan" in warning
    assert page.text(".dialog__name").endswith(".zip")


def test_the_filename_follows_the_client_name(page):
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    open_export(page)

    page.fill("#export-client", "Northbridge Mutual")
    name = page.text(".dialog__name")
    assert name.startswith("architecture-review-northbridge-mutual-")
    assert name.endswith(".zip")

    # The same rule as export.basename(), which is what the server will use.
    assert page.evaluate("exportSlug('  NHS Trust (Leeds)  ')") == "nhs-trust-leeds"


def test_one_self_contained_format_downloads_as_itself(page):
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    open_export(page)

    for format_id in ("pdf", "md"):
        page.click(f"[data-export-format='{format_id}']")
    page.wait_for("document.querySelectorAll('.format.is-on').length === 1")

    assert page.text(".dialog__name").endswith(".html")
    assert "Export 1 file" in page.text("[data-export-go]")


def test_choosing_nothing_leaves_nothing_to_press(page):
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    open_export(page)

    for format_id in ("pdf", "html", "md"):
        page.click(f"[data-export-format='{format_id}']")
    page.wait_for("document.querySelectorAll('.format.is-on').length === 0")

    assert page.evaluate("document.querySelector('[data-export-go]').disabled") is True


def test_the_dialog_closes_on_escape_and_on_the_backdrop(page):
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)

    open_export(page)
    page.evaluate(
        "document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))"
    )
    page.wait_for("!document.querySelector('.dialog')")

    open_export(page)
    page.click(".overlay")
    page.wait_for("!document.querySelector('.dialog')")


def test_cancelling_leaves_the_conversation_alone(page):
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    open_export(page)
    page.click("[data-export-cancel]")
    page.wait_for("!document.querySelector('.dialog')")

    assert page.count(".msg .reply") == 1
    assert page.evaluate("document.getElementById('export-btn').disabled") is False


def test_exporting_writes_a_real_file_and_says_so(page, live, tmp_path):
    """The whole path: fetch, blob, download, and the toast that follows it."""
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    page.send("Page.setDownloadBehavior", behavior="allow", downloadPath=str(downloads))

    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    open_export(page)
    page.fill("#export-client", "Northbridge Mutual")

    # The two text formats: printing a PDF inside a headless browser that is
    # already under test is a browser too many.
    page.click("[data-export-format='pdf']")
    page.wait_for("document.querySelectorAll('.format.is-on').length === 2")
    page.click("[data-export-go]")

    page.wait_for("!document.querySelector('.dialog')", timeout=60)
    landed = _downloaded(downloads)
    assert landed, "nothing arrived in the download folder"
    assert landed[0].name.startswith("architecture-review-northbridge-mutual-")
    assert landed[0].stat().st_size > 10_000

    with zipfile.ZipFile(landed[0]) as archive:
        names = [Path(name).name for name in archive.namelist()]
    assert "report.md" in names and "report.html" in names

    assert "Saved" in page.text(".toast")


# --------------------------------------------------------------------------- #
# Revising the architecture, and the dialog that asks for it
#
# The dialog used to let you choose formats without saying what was inside them.
# A thread whose conversation had moved on would export the opening guess, and
# nothing on screen said so. These drive the whole path in a real browser.
# --------------------------------------------------------------------------- #


def test_the_dialog_says_what_the_deliverable_covers(page):
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    open_export(page)

    subject = page.text(".subject")
    assert "Spread the load, cache the reads" in subject
    assert "eu-west-2" in subject
    assert f"{SERVICE_COUNT} services" in subject
    # Nothing has been asked since, so it says so rather than warning.
    assert "The architecture as it now stands." in subject
    assert not page.exists("[data-export-revise]")


def test_the_dialog_warns_when_the_conversation_has_moved_on(page):
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    page.ask("Would a read replica actually help here?")
    page.wait_for(SETTLED)
    page.ask("And make it two Graviton servers on Postgres.")
    page.wait_for(SETTLED)

    open_export(page)
    subject = page.text(".subject")

    assert "Out of date" in subject
    assert "2 questions have been asked since this architecture was written" in subject
    assert "is not in these files" in subject
    assert page.exists(".subject__stale [data-export-revise]")


def test_one_turn_is_counted_as_one(page):
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    page.ask("Would a read replica actually help?")
    page.wait_for(SETTLED)

    open_export(page)
    subject = page.text(".subject")

    assert "1 question has been asked since this architecture was written" in subject
    assert "Anything agreed in it" in subject


def test_revising_from_the_dialog_replaces_the_architecture(page, live):
    """The whole point: ask for it again, and the deliverable follows."""
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    page.ask("Two Graviton servers on Postgres, and drop the replica.")
    page.wait_for(SETTLED)

    open_export(page)
    page.click("[data-export-revise]")

    # The dialog closes and the revision streams into the thread. It does not
    # come back by itself: reading the revision before sending it to a client is
    # the point, and pressing Export again is what makes that happen.
    page.wait_for("!document.querySelector('.dialog')")
    page.wait_for(SETTLED, timeout=60)

    assert live.stub.calls[-1]["kind"] == "revision"

    # Two architectures on screen now, and the newer one is the revision.
    assert page.count(".rec__grid") == 2
    assert "Two Graviton servers, Postgres, and no read replica" in page.text(".thread")

    open_export(page)
    subject = page.text(".subject")
    assert "Two Graviton servers, Postgres, and no read replica" in subject
    assert "The architecture as it now stands." in subject
    assert not page.exists("[data-export-revise]")


def test_rebuilding_is_its_own_button_not_a_follow_up_chip(page, live):
    """It costs a full structured turn and it replaces what is on screen.

    Third in a row of casual prompts made it look like one, so it has a button of
    its own, above them and in the primary colour.
    """
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)

    assert "Revise the architecture" not in page.text(".suggestions")
    assert page.exists(".composer [data-rebuild]")
    # One word, and what it costs is the tooltip rather than a line of prose.
    assert page.text(".composer__rebuild").strip() == "Rebuild"
    assert "One full review" in page.evaluate("document.querySelector('.composer__rebuild').title")
    assert "One full review" not in page.evaluate("document.body.innerText")
    # The question sits between Rebuild and Send, all on one row.
    order = page.evaluate(
        "Array.from(document.querySelector('.composer').children)"
        "  .filter((n) => n.tagName === 'BUTTON' || n.tagName === 'INPUT')"
        "  .map((n) => n.id || n.className.split(' ').pop())"
    )
    assert order == ["composer__rebuild", "composer-input", "send-btn"]

    page.click("[data-rebuild]")
    page.wait_for(SETTLED, timeout=60)

    assert live.stub.calls[-1]["kind"] == "revision"
    # And it went in as a readable user turn, not as a hidden flag alone, so the
    # transcript records why the architecture was restated.
    asked = live.stub.calls[-1]["messages"][-1]["content"]
    assert (
        asked
        == "Revise the architecture to reflect everything we have agreed in this conversation."
    )


def test_there_is_nothing_to_rebuild_before_an_architecture_exists(page):
    """The button only appears once there is something for it to replace."""
    assert not page.exists("[data-rebuild]")
    # And the composer is just the question and Send until then.
    assert page.count(".composer__rebuild") == 0


def test_the_chips_ask_questions_rather_than_rebuilding(page, live):
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)

    # F13: the chips are the model's own, and each carries the words it sends.
    chips = page.evaluate(
        "Array.from(document.querySelectorAll('.suggestion')).map((n) => n.textContent)"
    )
    assert chips == NEXT_QUESTIONS

    page.click(f"[data-suggestion='{NEXT_QUESTIONS[0]}']")
    page.wait_for(SETTLED, timeout=60)

    assert live.stub.calls[-1]["kind"] == "follow_up"
    assert live.stub.calls[-1]["messages"][-1]["content"] == NEXT_QUESTIONS[0]


def test_the_chips_fall_back_to_the_generic_three(page, live):
    """An empty row where the guidance should be is worse than a generic one."""
    without = json.loads(FULL_JSON)
    without.pop("next_questions")
    live.stub.first = json.dumps(without)
    try:
        page.ask(WORKLOAD)
        page.wait_for(SETTLED)
        chips = page.evaluate(
            "Array.from(document.querySelectorAll('.suggestion')).map((n) => n.textContent)"
        )
    finally:
        live.stub.first = FULL_JSON

    assert chips == [
        "Break down the cost",
        "What are the risks?",
        "Compare with a serverless option",
    ]


# --------------------------------------------------------------------------- #
# The diagram as a file (F7)
# --------------------------------------------------------------------------- #


def test_the_diagram_downloads_as_a_vector_that_stands_on_its_own(page, tmp_path):
    """A file has no stylesheet around it, so it carries its own namespace and ground."""
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    page.send("Page.setDownloadBehavior", behavior="allow", downloadPath=str(downloads))

    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    page.click("[data-download-svg]")

    landed = _downloaded(downloads)
    assert landed, "nothing arrived in the download folder"
    assert landed[0].name.startswith("architecture-diagram-spread-the-load")
    assert landed[0].suffix == ".svg"

    svg = landed[0].read_text(encoding="utf-8")
    assert svg.startswith("<svg xmlns=")
    assert 'fill="#FFFFFF"' in svg  # a ground, so it is not drawn on nothing
    # The inline copy's scaling hook has no meaning in a file and is left off.
    assert "--natural-width" not in svg
    # And it is the whole graph, labels and all: every node, plus the ground.
    assert svg.count("<rect") == NODE_COUNT + GROUP_COUNT + 1
    assert ">https<" in svg


def test_the_diagram_downloads_as_a_raster_at_print_size(page, tmp_path):
    """Painted rather than rasterised, so the labels are in the page's own font."""
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    page.send("Page.setDownloadBehavior", behavior="allow", downloadPath=str(downloads))

    page.ask(WORKLOAD)
    page.wait_for(SETTLED)

    natural = page.evaluate("Number(document.querySelector('.diagram svg').getAttribute('width'))")
    scale = page.evaluate("(window.devicePixelRatio || 1) * 2")
    page.click("[data-download-png]")

    landed = _downloaded(downloads)
    assert landed, "nothing arrived in the download folder"
    assert landed[0].suffix == ".png"

    raw = landed[0].read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    # Width and height out of the IHDR, which is the first chunk of every PNG.
    width = int.from_bytes(raw[16:20], "big")
    height = int.from_bytes(raw[20:24], "big")
    assert width == round(natural * scale)
    assert height > 0


def test_the_download_buttons_are_not_the_source_toggle(page):
    """Three controls in one card, and each has to be reachable on its own."""
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)

    assert page.count(".diagram__head [data-download-svg]") == 1
    assert page.count(".diagram__head [data-download-png]") == 1
    # The source toggle keeps its own class, and its own place under the card.
    assert page.count(".diagram__toggle") == 1
    assert page.evaluate("document.querySelector('.diagram__toggle').dataset.source !== undefined")


# --------------------------------------------------------------------------- #
# The assumptions panel (F10)
# --------------------------------------------------------------------------- #


# Setting the value and pressing Enter, which is what a reader does. `fill` only
# dispatches `input`, and it is the keydown that commits.
def correct(page, index, text):
    opened = page.evaluate(
        "(() => { const rows = document.querySelectorAll('.assumption');"
        f" const row = rows[{index}];"
        " const button = row && row.querySelector('[data-assumption]');"
        " if (!button) return false; button.click(); return true; })()"
    )
    assert opened, f"no assumption to edit at row {index}"
    page.wait_for("!!document.querySelector('[data-assumption-input]')")
    page.evaluate(
        "(() => { const box = document.querySelector('[data-assumption-input]');"
        f" box.value = {text!r};"
        " box.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));"
        " return true; })()"
    )


def test_the_assumptions_are_shown_as_the_model_wrote_them(page):
    """F10: what the sizing rests on, on screen rather than buried in the prose."""
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)

    shown = page.evaluate(
        "Array.from(document.querySelectorAll('.assumption__text')).map((n) => n.textContent)"
    )
    assert shown == ASSUMPTIONS
    # Above the services, where the field sits in the schema.
    assert page.evaluate(
        "document.querySelector('.assumptions').compareDocumentPosition("
        "  document.querySelector('.services')) === Node.DOCUMENT_POSITION_FOLLOWING"
    )


def test_correcting_assumptions_stages_them_and_spends_nothing(page, live):
    """Several can be put right in one pass before a rebuild is asked for."""
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    assert live.stub.count == 1

    correct(page, 0, "About 9,000 orders a day, ten times that at Christmas.")
    page.wait_for("!!document.querySelector('.assumption--changed')")
    correct(page, 2, "It has to be up around the clock.")
    page.wait_for("document.querySelectorAll('.assumption--changed').length === 2")

    # Nothing has been asked of the API, and the originals are still readable.
    assert live.stub.count == 1
    was = page.evaluate(
        "Array.from(document.querySelectorAll('.assumption__was')).map((n) => n.textContent)"
    )
    assert was == [f"was: {ASSUMPTIONS[0]}", f"was: {ASSUMPTIONS[2]}"]
    # And the gap between what is on screen and what the reader says is stated.
    assert "2 assumptions have been corrected" in page.text(".stale")
    assert page.exists("[data-rebuild-assumptions]")


def test_correcting_an_assumption_does_not_rebuild_the_thread(page):
    """A2: the panel repaints, the messages above it are left where they are."""
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    page.evaluate(MARK)

    correct(page, 0, "About 9,000 orders a day.")
    page.wait_for("!!document.querySelector('.assumption--changed')")

    assert page.evaluate(MARKED) is True


def test_an_assumption_put_back_as_it_was_is_not_a_correction(page):
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)

    correct(page, 1, "Something else entirely.")
    page.wait_for("!!document.querySelector('.stale')")

    correct(page, 1, ASSUMPTIONS[1])
    page.wait_for("!document.querySelector('.assumption--changed')")
    assert not page.exists("[data-rebuild-assumptions]")


def test_correcting_assumptions_costs_one_revision_however_many_changed(page, live):
    """The whole reason they stage: three corrections are still one review."""
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)

    correct(page, 0, "About 9,000 orders a day.")
    page.wait_for("!!document.querySelector('.assumption--changed')")
    correct(page, 1, "Around 400 GB of history.")
    page.wait_for("document.querySelectorAll('.assumption--changed').length === 2")

    page.click("[data-rebuild-assumptions]")
    page.wait_for(SETTLED, timeout=60)

    assert live.stub.count == 2
    assert live.stub.calls[-1]["kind"] == "revision"
    # It went in as a readable user turn naming both the new figure and the old,
    # so the transcript records what was corrected rather than only the result.
    asked = live.stub.calls[-1]["messages"][-1]["content"]
    assert asked.startswith("These assumptions are wrong.")
    assert "- About 9,000 orders a day." in asked
    assert f"(you had: {ASSUMPTIONS[0]})" in asked
    assert "- Around 400 GB of history." in asked
    # And the notice has gone with the reply that answered it.
    assert not page.exists("[data-rebuild-assumptions]")


def test_only_the_newest_architecture_can_be_corrected(page):
    """A superseded reply is still readable; correcting it would act on nothing."""
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)

    correct(page, 0, "About 9,000 orders a day.")
    page.wait_for("!!document.querySelector('[data-rebuild-assumptions]')")
    page.click("[data-rebuild-assumptions]")
    page.wait_for(SETTLED, timeout=60)

    assert page.count(".assumptions") == 2
    assert page.count("[data-assumption]") == len(ASSUMPTIONS)
    # The editable one is the second panel, not the first.
    assert page.evaluate(
        "document.querySelectorAll('.assumptions')[1].contains("
        "  document.querySelector('[data-assumption]'))"
    )


# --------------------------------------------------------------------------- #
# One cost figure, not two (F12)
# --------------------------------------------------------------------------- #

# Seeded rather than waited for throughout: a real estimate reads a 200 MB file
# from AWS, and what these check is the element, not the arithmetic.
SEED = """(() => {
  const session = activeSession();
  const index = session.messages.findIndex(
    (m) => m.role === 'assistant' && m.structured
  );
  const id = `${session.id}-${index}`;
  state.estimates[id] = Object.assign({
    region: 'eu-west-2', tier: 'Medium', claimedTier: 'Medium', tierGap: 0,
    priced: true, hasFigure: true, anyEstimated: true,
    monthlyUsd: 1000, pricedUsd: PRICED, estimatedUsd: ESTIMATED,
    unpricedServices: 1, estimatedServices: 1,
    linesPriced: 6, linesEstimated: 2, linesTotal: 8,
    services: [
      {name: 'RDS', monthlyUsd: PRICED, priced: true, estimated: false,
       lines: [{detail: '1 x $0.3520/instance-hour x 730h'}]},
      {name: 'CloudFront', monthlyUsd: ESTIMATED, priced: false, estimated: true,
       lines: [{detail: "not priced here; the advisor's own estimate"}]},
    ],
  }, {});
  paintEstimate(id);
  return id;
})()"""


def seed_estimate(page, priced: float, estimated: float) -> str:
    """One cost element on screen, split however the test needs it."""
    return page.evaluate(SEED.replace("PRICED", str(priced)).replace("ESTIMATED", str(estimated)))


def test_the_cost_is_one_element_carrying_one_figure(page):
    """The whole of F12 in one assertion: two panels became one."""
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    seed_estimate(page, 700, 300)

    assert page.count(".estimate") == 1
    assert page.count(".cost-panel") == 0
    assert page.count(".cost-inline") == 0
    # The band is the computed one, sitting on the figure rather than beside it
    # in a currency of its own.
    assert "Medium" in page.text(".estimate__band")
    assert "$1,000.00" in page.text(".estimate__total")


def test_a_blended_total_says_it_is_estimated(page):
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    seed_estimate(page, 700, 300)

    assert "Estimated monthly cost" in page.text(".estimate__head")
    assert "6 of 8 lines from the AWS Price List" in page.text(".estimate__meta")
    assert "CloudFront" in page.text(".estimate__lines")
    assert "est." in page.text(".estimate__lines")


def test_a_total_with_nothing_estimated_is_stated_outright(page):
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    page.evaluate(
        """(() => {
          const session = activeSession();
          const index = session.messages.findIndex(
            (m) => m.role === 'assistant' && m.structured
          );
          const id = `${session.id}-${index}`;
          state.estimates[id] = {
            region: 'eu-west-2', tier: 'Medium', tierGap: 0,
            priced: true, hasFigure: true, anyEstimated: false,
            monthlyUsd: 1000, pricedUsd: 1000, estimatedUsd: 0,
            linesPriced: 8, linesEstimated: 0, linesTotal: 8,
            services: [{name: 'RDS', monthlyUsd: 1000, priced: true,
                        estimated: false, lines: []}],
          };
          paintEstimate(id);
          return true;
        })()"""
    )

    head = page.text(".estimate__head")
    assert "Monthly cost" in head
    assert "Estimated" not in head
    assert "every line from the AWS Price List" in page.text(".estimate__meta")


def test_the_breakdown_folds_away_when_most_of_the_money_is_a_guess(page):
    """Share of dollars, not share of lines: it is the money being judged."""
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)

    seed_estimate(page, 900, 100)  # a tenth estimated
    assert page.exists(".estimate__lines")

    seed_estimate(page, 100, 900)  # nine tenths estimated
    assert not page.exists(".estimate__lines")
    assert "Show the" in page.text(".estimate__toggle")


def test_the_reader_can_open_the_breakdown_and_it_stays_open(page):
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    identifier = seed_estimate(page, 100, 900)

    assert not page.exists(".estimate__lines")
    page.click(".estimate__toggle")
    assert page.exists(".estimate__lines")

    # A repaint is not a reset: the choice lives in state, not in the DOM.
    page.evaluate(f"paintEstimate({identifier!r})")
    assert page.exists(".estimate__lines")


def test_a_figure_priced_before_the_last_answers_says_so(page, live):
    """A prose follow-up cannot reprice, so the figure is marked rather than moved."""
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    seed_estimate(page, 700, 300)
    assert not page.exists(".estimate__flag--stale")

    page.ask("Does the read replica earn its keep?")
    page.wait_for(SETTLED, timeout=60)

    stale = page.text(".estimate__flag--stale")
    assert "Priced before the last answer" in stale
    assert "Rebuild the architecture to reprice it" in stale


def test_a_band_a_full_band_from_the_arithmetic_is_flagged(page):
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    page.evaluate(
        """(() => {
          const session = activeSession();
          const index = session.messages.findIndex(
            (m) => m.role === 'assistant' && m.structured
          );
          const id = `${session.id}-${index}`;
          state.estimates[id] = {
            region: 'eu-west-2', tier: 'High', claimedTier: 'Low', tierGap: 2,
            priced: true, hasFigure: true, anyEstimated: false,
            monthlyUsd: 9000, pricedUsd: 9000, estimatedUsd: 0,
            linesPriced: 8, linesEstimated: 0, linesTotal: 8,
            services: [{name: 'RDS', monthlyUsd: 9000, priced: true,
                        estimated: false, lines: []}],
          };
          paintEstimate(id);
          return true;
        })()"""
    )

    flag = page.text(".estimate__flag")
    assert "own read of this was" in flag
    assert "mis-sized" in flag


def test_the_dialog_shows_the_money_once_it_has_been_priced(page):
    """Seeded rather than waited for: pricing reads a 200 MB file from AWS."""
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)

    page.evaluate(
        """(() => {
          const session = activeSession();
          const index = session.messages.findIndex(
            (m) => m.role === 'assistant' && m.structured
          );
          state.estimates[`${session.id}-${index}`] = {
            priced: true, hasFigure: true, anyEstimated: true,
            monthlyUsd: 1695, estimatedUsd: 42.5,
            unpricedServices: 1, estimatedServices: 1,
            region: 'eu-west-2', services: [],
          };
          return true;
        })()"""
    )
    open_export(page)
    subject = page.text(".subject")

    assert "$1,695.00/mo estimated" in subject
    assert "$42.50 of the $1,695.00 total is the advisor's own estimate" in subject
    assert "1 of 7 services" in subject


def test_the_dialog_says_when_there_is_no_estimate_to_print(page):
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)

    page.evaluate(
        """(() => {
          const session = activeSession();
          const index = session.messages.findIndex(
            (m) => m.role === 'assistant' && m.structured
          );
          state.estimates[`${session.id}-${index}`] =
            { priced: false, hasFigure: false, services: [] };
          return true;
        })()"""
    )
    open_export(page)

    assert "carries its band alone, unchecked" in page.text(".subject")


def _downloaded(folder: Path, timeout: float = 60.0, expected: int = 1) -> list[Path]:
    """Wait for Chrome to finish writing the files it was given.

    A download in flight is marked `.crdownload`, but there is a moment where the
    real name exists and the bytes have not landed yet, so size is part of "done":
    without it a test can read a file Chrome is still filling.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        done = [
            item
            for item in folder.iterdir()
            if item.is_file()
            and not item.name.endswith(".crdownload")
            and item.stat().st_size > 1024
        ]
        if len(done) >= expected:
            return done
        time.sleep(0.2)
    return []


def test_exporting_twice_in_one_session_saves_two_files(page, live, tmp_path):
    """A consultant exports one conversation after another without reloading."""
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    page.send("Page.setDownloadBehavior", behavior="allow", downloadPath=str(downloads))

    page.ask(WORKLOAD)
    page.wait_for(SETTLED)

    for client in ("First Client", "Second Client"):
        open_export(page)
        page.fill("#export-client", client)
        page.click("[data-export-format='pdf']")
        page.wait_for("document.querySelectorAll('.format.is-on').length === 2")
        page.click("[data-export-go]")
        page.wait_for("!document.querySelector('.dialog')", timeout=60)

    landed = _downloaded(downloads, expected=2)
    names = sorted(item.name for item in landed)
    assert len(names) == 2, names
    assert any("first-client" in name for name in names)
    assert any("second-client" in name for name in names)
    for item in landed:
        with zipfile.ZipFile(item) as archive:
            assert any(name.endswith("report.md") for name in archive.namelist())


def test_an_export_that_fails_says_why_and_keeps_the_dialog_open(page, live):
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    open_export(page)

    # Make the server refuse it, the way a browserless machine refuses a PDF.
    page.evaluate(
        "window.fetch = async () => new Response("
        "JSON.stringify({error: 'No browser was found to print the PDF with.',"
        " kind: 'no_browser'}), {status: 502,"
        " headers: {'Content-Type': 'application/json'}}); true"
    )
    page.click("[data-export-go]")
    page.wait_for("!!document.querySelector('.dialog__note--error')")

    assert "No browser was found" in page.text(".dialog__note--error")
    assert page.exists(".dialog") is True


# --------------------------------------------------------------------------- #
# More than two columns (F9)
# --------------------------------------------------------------------------- #


def test_a_comparison_can_be_widened_to_four(page, live):
    page.click("#tab-compare")
    page.wait_for("!!document.querySelector('#workload-0')")

    page.click("#add-workload")
    page.wait_for("!!document.querySelector('#workload-2')")
    page.click("#add-workload")
    page.wait_for("!!document.querySelector('#workload-3')")
    # Four is the ceiling, so the button goes rather than offering a fifth.
    assert not page.exists("#add-workload")

    for index, text in enumerate(("Serverless", "On EC2", "On ECS", "On Lambda")):
        page.fill(f"#workload-{index}", text)
    page.click("#compare-btn")
    page.wait_for(SETTLED, timeout=30)

    badges = page.evaluate(
        "Array.from(document.querySelectorAll('.option__badge')).map((n) => n.textContent)"
    )
    assert badges == ["Option A", "Option B", "Option C", "Option D"]
    assert live.stub.count == 4
    # Every column keeps its own place in the grid, rather than the third
    # painting over the first.
    assert page.count(".compare__results--4") == 1


def test_a_fourth_column_is_its_own_column_not_the_first_again(page, live):
    """The index used to be clamped to 0 or 1, which lost the extra replies."""
    live.stub.delay = 0.2
    page.click("#tab-compare")
    page.wait_for("!!document.querySelector('#workload-0')")
    page.click("#add-workload")
    page.wait_for("!!document.querySelector('#workload-2')")

    for index, text in enumerate(("A way", "B way", "C way")):
        page.fill(f"#workload-{index}", text)
    page.click("#compare-btn")
    page.wait_for("document.querySelectorAll('.option').length === 3")
    page.wait_for(SETTLED, timeout=30)

    columns = page.evaluate(
        "Array.from(document.querySelectorAll('.option')).map("
        "  (n) => n.style.getPropertyValue('--option-column'))"
    )
    assert columns == ["1", "2", "3"]


def test_a_workload_can_be_dropped_back_to_two(page):
    page.click("#tab-compare")
    page.wait_for("!!document.querySelector('#workload-0')")
    page.click("#add-workload")
    page.wait_for("!!document.querySelector('#workload-2')")

    page.click("[data-drop-workload='2']")
    page.wait_for("!document.querySelector('#workload-2')")
    # Two is the floor: neither of the first two offers a Remove button.
    assert page.count("[data-drop-workload]") == 0


# --------------------------------------------------------------------------- #
# Region and compliance (F5)
# --------------------------------------------------------------------------- #


def test_the_region_and_the_regime_are_sent_with_the_question(page, live):
    page.wait_for("!!document.querySelector('#region-advisor')")
    page.evaluate(
        "const r = document.getElementById('region-advisor');"
        "r.value = 'eu-west-1';"
        "r.dispatchEvent(new Event('change', { bubbles: true }));"
        "const c = document.getElementById('compliance-advisor');"
        "c.value = 'nhs-dspt';"
        "c.dispatchEvent(new Event('change', { bubbles: true }));"
        "true"
    )
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)

    constraints = live.stub.calls[-1]["constraints"]
    assert "Build this in eu-west-1." in constraints
    assert "Data Security and Protection Toolkit" in constraints


def test_the_selectors_are_disabled_while_a_reply_arrives(page, live):
    live.stub.delay = 0.4
    page.wait_for("!!document.querySelector('#region-advisor')")
    page.fill("#composer-input", WORKLOAD)
    page.click("#send-btn")

    page.wait_for("document.body.classList.contains('is-busy')")
    assert page.evaluate("document.getElementById('region-advisor').disabled") is True
    page.wait_for(SETTLED, timeout=30)


# --------------------------------------------------------------------------- #
# Pillar links and coverage (F6)
# --------------------------------------------------------------------------- #


def test_each_pillar_links_to_the_framework_and_coverage_is_stated(page):
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)

    names = page.evaluate(
        "Array.from(document.querySelectorAll('.pillar')).map((n) => n.textContent)"
    )
    assert "Performance Efficiency" in names
    assert "Cost Optimization" in names

    hrefs = page.evaluate(
        "Array.from(document.querySelectorAll('a.pillar')).map((n) => n.getAttribute('href'))"
    )
    assert hrefs and all(
        href.startswith("https://docs.aws.amazon.com/wellarchitected/") for href in hrefs
    )

    # The sample answers four of the six, and says which two it did not.
    coverage = page.text(".coverage")
    assert "4 / 6 pillars" in coverage
    assert "Operations" in coverage and "Sustainability" in coverage


# --------------------------------------------------------------------------- #
# Finding a saved conversation (F8)
# --------------------------------------------------------------------------- #


def test_searching_narrows_the_saved_list(page, live):
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    page.click("#save-btn")
    page.wait_for("document.querySelectorAll('#saved-list .saved-item').length === 1")

    page.fill("#saved-search", "Black Friday")
    page.wait_for("document.querySelectorAll('#saved-list .saved-item').length === 1")

    page.fill("#saved-search", "submarines")
    page.wait_for("!!document.querySelector('#saved-list .sidebar__empty')")
    assert "Nothing matched" in page.text("#saved-list")

    page.click("[data-clear-filters]")
    page.wait_for("document.querySelectorAll('#saved-list .saved-item').length === 1")


def test_a_cost_band_filter_is_offered_and_narrows(page):
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    page.click("#save-btn")
    page.wait_for("!!document.querySelector(\"[data-filter-tier='Medium']\")")

    page.click("[data-filter-tier='Medium']")
    page.wait_for("document.querySelectorAll('#saved-list .saved-item').length === 1")
    assert page.count(".filter.is-on") == 1

    # Clicking the filter that is on takes it off again.
    page.click("[data-filter-tier='Medium']")
    page.wait_for("document.querySelectorAll('.filter.is-on').length === 0")


def test_a_conversation_can_be_tagged_and_filtered_by_the_tag(page):
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    page.click("#save-btn")
    page.wait_for("document.querySelectorAll('#saved-list .saved-item').length === 1")

    conversation_id = store.listing()[0]["id"]
    page.click(f"[data-tag='{conversation_id}']")
    page.wait_for("!!document.querySelector('[data-tag-input]')")
    page.fill("[data-tag-input]", "NHS")
    page.evaluate(
        "document.querySelector('[data-tag-input]').dispatchEvent("
        "  new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));true"
    )

    page.wait_for("!!document.querySelector('#saved-list .tag')")
    assert "nhs" in page.text("#saved-list .tag")

    page.click("[data-filter-tag='nhs']")
    page.wait_for("document.querySelectorAll('#saved-list .saved-item').length === 1")


def test_the_constraints_open_the_conversation_rather_than_crowd_the_composer(page):
    """They are settled once and then read, so they belong at the top."""
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)

    assert page.evaluate(
        "const row = document.querySelector('.constraints');"
        "const thread = document.querySelector('.thread');"
        "row.compareDocumentPosition(thread) & Node.DOCUMENT_POSITION_FOLLOWING"
    )
    # Not tucked inside the composer, where it used to be.
    assert page.count(".composer .constraints") == 0


def test_changing_the_region_mid_conversation_says_to_rebuild(page, live):
    """A follow-up cannot move an architecture, so the gap is stated rather than hidden."""
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)
    assert not page.exists(".stale")

    page.evaluate(
        "const r = document.getElementById('region-advisor');"
        "r.value = 'us-east-1';"
        "r.dispatchEvent(new Event('change', { bubbles: true }));true"
    )
    page.wait_for("!!document.querySelector('.stale')")

    notice = page.text(".stale")
    assert "Rebuild before you carry on" in notice
    assert "N. Virginia (us-east-1)" in notice
    # Nothing is blocked: a question before rebuilding is still a fair question.
    assert page.evaluate("document.getElementById('send-btn').disabled") is False

    page.click(".stale [data-rebuild]")
    page.wait_for(SETTLED, timeout=60)

    # Rebuilt to what is selected now, so the two agree again and the notice goes.
    assert live.stub.calls[-1]["kind"] == "revision"
    assert "Build this in us-east-1." in live.stub.calls[-1]["constraints"]
    assert not page.exists(".stale")


def test_a_reply_that_matches_what_is_selected_says_nothing(page):
    page.wait_for("!!document.querySelector('#region-advisor')")
    page.evaluate(
        "const r = document.getElementById('region-advisor');"
        "r.value = 'eu-west-2';"
        "r.dispatchEvent(new Event('change', { bubbles: true }));true"
    )
    page.ask(WORKLOAD)
    page.wait_for(SETTLED)

    assert not page.exists(".stale")


def test_the_sidebar_no_longer_carries_the_disclaimer(page):
    """Removed to give the saved list the room."""
    assert not page.exists(".sidebar__foot")
    assert "advisory and should be reviewed" not in page.evaluate("document.body.innerText")
    # The three buttons sit on the bottom edge of the sidebar.
    assert page.exists(".sidebar__actions #export-btn")
