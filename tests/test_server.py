"""server.py: every endpoint, its error paths, and the guards around them."""

import io
import json
import sqlite3
import time
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

import advisor
import branding
import server
import store
from advisor import AdvisorError
from tests.conftest import final_event, sse_events
from tests.samples import FULL_JSON, LEGACY_REPLY, REVISED_JSON, SERVICE_COUNT

ROOT = Path(__file__).resolve().parent.parent


def advise(client, messages):
    """Ask a question and read the whole event stream back."""
    return client.post("/api/advise", json={"messages": messages})


def ask(client, text="A shop"):
    """One first question, and the events it produced."""
    return sse_events(advise(client, [{"role": "user", "content": text}]))


def save_one(client, title="NHS records", reply=FULL_JSON, **extra):
    """Save a two-message conversation and return its id."""
    body = {
        "messages": [
            {"role": "user", "content": "A patient records system for an NHS trust with users"},
            {"role": "assistant", "content": reply},
        ],
        "mode": "advise",
        **extra,
    }
    if title is not None:
        body["title"] = title
    response = client.post("/api/save", json=body)
    assert response.status_code == 200
    return response.get_json()["id"]


# --------------------------------------------------------------------------- #
# The front end and its headers
# --------------------------------------------------------------------------- #


def test_the_page_and_its_assets_are_served(client):
    page = client.get("/")
    assert page.status_code == 200
    assert b"Architecture Advisor" in page.data

    assert client.get("/app.js").status_code == 200
    assert client.get("/app.css").status_code == 200
    assert client.get("/nope.txt").status_code == 404


def test_the_palette_is_served_from_branding(client):
    """The colours are generated, not a file in static/, so that branding.py is
    the only place any of them is written down (A5)."""
    response = client.get("/brand.css")

    assert response.status_code == 200
    assert response.mimetype == "text/css"

    stylesheet = response.get_data(as_text=True)
    assert f"--cs-primary: {branding.BRAND_PRIMARY};" in stylesheet
    assert stylesheet == branding.css_root()

    # The page has to ask for it, and before app.css, which reads it.
    page = client.get("/").get_data(as_text=True)
    assert page.index('href="brand.css"') < page.index('href="app.css"')


def test_brand_assets_are_cached_for_a_week(client):
    response = client.get("/assets/mark-placeholder.svg")
    assert response.status_code == 200
    assert f"max-age={server.ASSET_MAX_AGE}" in response.headers["Cache-Control"]


@pytest.mark.parametrize("path", ["/", "/api/conversations"])
def test_every_response_carries_the_security_headers(client, path):
    """S1: defence in depth for the HTML the front end injects."""
    headers = client.get(path).headers
    csp = headers["Content-Security-Policy"]

    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    # The diagram SVG carries one inline style attribute, so this one is needed.
    assert "style-src 'self' 'unsafe-inline'" in csp
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"


# --------------------------------------------------------------------------- #
# Health and setup guide
# --------------------------------------------------------------------------- #


def test_health_reports_a_usable_key(client):
    body = client.get("/api/health").get_json()
    assert body["ok"] is True
    assert body["model"] == advisor.MODEL


def test_no_endpoint_gives_away_who_is_running_it(client):
    """S5: /api/health used to return the machine's account name."""
    import getpass

    account = getpass.getuser()
    for path in ("/api/health", "/api/conversations", "/api/readme"):
        assert account.lower() not in client.get(path).get_data(as_text=True).lower()
    assert "user" not in client.get("/api/health").get_json()


def test_health_reports_a_missing_key_without_failing(client, monkeypatch):
    monkeypatch.setattr(
        server, "get_client", lambda: (_ for _ in ()).throw(AdvisorError("no key", "missing_key"))
    )
    body = client.get("/api/health").get_json()
    assert body["ok"] is False
    assert body["kind"] == "missing_key"


def test_the_readme_is_rendered_once_and_reused(client):
    first = client.get("/api/readme").get_json()["html"]
    assert "<h2>" in first
    assert client.get("/api/readme").get_json()["html"] == first


def test_a_missing_readme_is_explained_not_a_500(client, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "README", tmp_path / "gone.md")
    monkeypatch.setattr(server, "_readme_cache", None)
    assert "No README.md" in client.get("/api/readme").get_json()["html"]


# --------------------------------------------------------------------------- #
# /api/advise
# --------------------------------------------------------------------------- #


def test_a_first_question_returns_a_parsed_recommendation(client, api):
    response = advise(client, [{"role": "user", "content": "A shop"}])
    assert response.status_code == 200
    assert response.mimetype == "text/event-stream"

    done = final_event(response)
    assert done["type"] == "done"
    assert done["message"]["structured"] is True
    assert done["message"]["headline"].startswith("Spread the load")
    assert len(done["message"]["services"]) == SERVICE_COUNT
    assert done["message"]["diagram"]["nodes"]
    assert done["message"]["raw"] == FULL_JSON
    # R5: a first recommendation is the job the deeper setting is for.
    assert api.calls[0]["kind"] == "recommendation"


def test_a_follow_up_is_prose_and_costs_less_thought(client, api):
    thread = [
        {"role": "user", "content": "A shop"},
        {"role": "assistant", "content": FULL_JSON},
        {"role": "user", "content": "How do I cut the cost?"},
    ]
    done = final_event(advise(client, thread))

    assert done["message"]["structured"] is False
    assert "<ul>" in done["message"]["prose"]
    assert api.calls[0]["kind"] == "follow_up"


def test_the_reply_names_the_conversation(client):
    """A5: the browser used to work the title out itself, from its own copy of the rule."""
    done = final_event(advise(client, [{"role": "user", "content": "A" * 60}]))
    assert done["title"] == store.title_from("A" * 60)


def test_a_comparison_names_itself_too(client):
    done = final_event(client.post("/api/compare", json={"workloads": ["Serverless", "On EC2"]}))
    assert done["title"] == "Compare: Serverless"


def test_every_call_lands_in_the_ledger(client):
    """A1 and S4: what was spent is recorded whether or not it is ever saved."""
    ask(client)  # reading the stream is what runs it
    assert store.spent_on()["calls"] == 1

    sse_events(client.post("/api/compare", json={"workloads": ["a", "b"]}))
    assert store.spent_on()["calls"] == 3
    assert store.spent_on()["tokens"] > 0


def test_a_call_is_counted_even_when_the_browser_walks_away(client, api):
    """The runaway tab this ceiling exists for is one that never reads a reply.

    A browser that navigates away closes the generator feeding it, so anything
    written to the ledger from there would be skipped for exactly the calls that
    matter most. They are billed all the same.
    """
    api.delay = 0.05  # still running when the reader gives up
    response = advise(client, [{"role": "user", "content": "A shop"}])

    stream = response.iter_encoded()
    next(stream)  # one frame, then the tab is gone
    stream.close()
    response.close()

    deadline = time.monotonic() + 5
    while store.spent_on()["calls"] == 0 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert store.spent_on()["calls"] == 1


def test_a_call_that_failed_is_still_counted(client, api):
    api.raise_with = AdvisorError(
        "cut short", kind="truncated", usage={"calls": 1, "costUsd": 0.02}
    )
    ask(client)
    assert store.spent_on()["calls"] == 1


def test_the_reply_carries_what_the_call_cost(client):
    """R2: the browser adds this to its running total."""
    usage = final_event(advise(client, [{"role": "user", "content": "A"}]))["usage"]

    assert usage["calls"] == 1
    assert usage["inputTokens"] > 0
    assert usage["costUsd"] > 0
    assert usage["priced"] is True


# --------------------------------------------------------------------------- #
# The event stream (R3)
# --------------------------------------------------------------------------- #


def test_reasoning_arrives_before_any_of_the_answer(client):
    """The point of streaming: something to show during the long first wait."""
    events = ask(client)
    kinds = [event["type"] for event in events]

    assert kinds[0] == "thinking"
    assert kinds[-1] == "done"
    assert "partial" in kinds
    # A thinking line is one line, and it is not the whole reasoning dumped out.
    thinking = events[0]["text"]
    assert "\n" not in thinking
    assert len(thinking) <= server.MAX_THINKING_CHARS


def test_a_recommendation_is_drawn_as_it_is_written(client):
    partials = [event for event in ask(client) if event["type"] == "partial"]

    assert partials
    early = partials[0]["message"]
    assert early["structured"] is True
    assert early["headline"]
    # Only what has actually arrived: the fields written last are not there yet.
    assert len(early["services"]) < SERVICE_COUNT
    assert early["cost"] is None


def test_a_follow_up_streams_rendered_markdown(client):
    thread = [
        {"role": "user", "content": "A shop"},
        {"role": "assistant", "content": FULL_JSON},
        {"role": "user", "content": "Is the replica worth it?"},
    ]
    partials = [event for event in sse_events(advise(client, thread)) if event["type"] == "partial"]
    assert partials
    assert partials[0]["message"]["structured"] is False
    assert "<p>" in partials[0]["message"]["prose"]


def test_a_long_thinking_line_is_cut_rather_than_wrapped():
    assert server._thinking_line("") == ""
    assert server._thinking_line("  \n  ") == ""
    assert server._thinking_line("first\nsecond") == "second"

    long_line = "x" * (server.MAX_THINKING_CHARS + 50)
    cut = server._thinking_line(long_line)
    assert len(cut) == server.MAX_THINKING_CHARS
    assert cut.endswith("\u2026")


@pytest.mark.parametrize(
    "messages",
    [
        [],
        ["junk"],
        [{"role": "user", "content": "   "}],
        [{"role": "system", "content": "hi"}],
        [{"role": "assistant", "content": "a reply with no question"}],
        [{"role": "user", "content": "A"}, {"role": "assistant", "content": "B"}],
    ],
)
def test_a_conversation_that_makes_no_sense_is_refused(client, api, messages):
    response = client.post("/api/advise", json={"messages": messages})
    assert response.status_code == 400
    assert response.get_json()["kind"] == "empty"
    assert api.count == 0


def test_a_failure_from_the_api_keeps_its_kind_and_its_cost(client, api):
    """Once the stream has started there are no status codes left, only events."""
    api.raise_with = AdvisorError(
        "cut short", kind="truncated", usage={"calls": 1, "costUsd": 0.02}
    )
    response = advise(client, [{"role": "user", "content": "A"}])

    assert response.status_code == 200
    failure = final_event(response)
    assert failure["type"] == "error"
    assert failure["kind"] == "truncated"
    assert failure["usage"]["costUsd"] == 0.02


def test_a_rate_limit_tells_the_browser_how_long_to_wait(client, api):
    """R7: the retry-after window is no use if it stops at the server."""
    api.raise_with = AdvisorError("slow down", kind="rate_limit", retry_after=30)
    failure = final_event(advise(client, [{"role": "user", "content": "A"}]))

    assert failure["kind"] == "rate_limit"
    assert failure["retryAfter"] == 30


def test_a_bug_in_a_call_ends_the_stream_rather_than_hanging_it(client, api):
    api.raise_with = ValueError("something nobody expected")
    failure = final_event(advise(client, [{"role": "user", "content": "A"}]))

    assert failure["type"] == "error"
    assert failure["kind"] == "error"


def test_a_missing_key_stops_the_request_before_the_api(client, api, monkeypatch):
    monkeypatch.setattr(
        server, "get_client", lambda: (_ for _ in ()).throw(AdvisorError("no key", "missing_key"))
    )
    response = client.post("/api/advise", json={"messages": [{"role": "user", "content": "A"}]})

    assert response.status_code == 400
    assert response.get_json()["kind"] == "missing_key"
    assert api.count == 0


# --------------------------------------------------------------------------- #
# Request limits (S2)
# --------------------------------------------------------------------------- #


def test_an_oversized_body_is_refused_as_json(client, api):
    """Flask's own limit, answered in the shape the front end understands."""
    body = json.dumps({"messages": [{"role": "user", "content": "x" * (2 * 1024 * 1024)}]})
    response = client.post("/api/advise", data=body, content_type="application/json")

    assert response.status_code == 413
    assert response.get_json()["kind"] == "too_long"
    assert api.count == 0


@pytest.mark.parametrize(
    ("messages", "phrase"),
    [
        ([{"role": "user", "content": "x" * 25_000}], "characters long"),
        ([{"role": "user", "content": "x"}] * 80, "messages long"),
        ([{"role": "user", "content": "x" * 19_000}] * 40, "characters long"),
    ],
)
def test_a_conversation_over_the_caps_never_reaches_the_api(client, api, messages, phrase):
    response = client.post("/api/advise", json={"messages": messages})

    assert response.status_code == 400
    assert response.get_json()["kind"] == "too_long"
    assert phrase in response.get_json()["error"]
    assert api.count == 0


def test_a_long_thread_is_measured_in_tokens_before_it_is_sent(client, api, monkeypatch):
    """Characters are the cheap guard; the token count is the accurate one."""
    counted = {}

    def fake_count(client_, messages, kind="recommendation"):
        counted["messages"] = len(messages)
        counted["kind"] = kind
        return server.MAX_INPUT_TOKENS + 1

    monkeypatch.setattr(server, "count_input_tokens", fake_count)
    long_thread = [{"role": "user", "content": "x" * 19_000}] * 5

    response = client.post("/api/advise", json={"messages": long_thread})
    assert response.status_code == 400
    assert "tokens of input" in response.get_json()["error"]
    assert counted["messages"] == 5
    assert api.count == 0


def test_a_short_thread_is_not_worth_counting(client, api, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("counted tokens for a short conversation")

    monkeypatch.setattr(server, "count_input_tokens", fail)
    assert (
        client.post(
            "/api/advise", json={"messages": [{"role": "user", "content": "A"}]}
        ).status_code
        == 200
    )


# --------------------------------------------------------------------------- #
# /api/compare
# --------------------------------------------------------------------------- #


def test_comparing_runs_both_sides_and_adds_up_the_cost(client, api):
    response = client.post("/api/compare", json={"workloads": ["Serverless", "On EC2"]})
    done = final_event(response)

    assert response.status_code == 200
    assert done["type"] == "done"
    assert len(done["results"]) == 2
    assert all(result["structured"] for result in done["results"])
    assert api.count == 2
    assert done["usage"]["calls"] == 2
    assert {call["kind"] for call in api.calls} == {"comparison"}


@pytest.mark.parametrize("width", [2, 3, 4])
def test_a_comparison_can_be_wider_than_two(client, api, width):
    """F9: the UI was hardcoded to two columns and so was this."""
    workloads = [f"Option {n}" for n in range(width)]
    done = final_event(client.post("/api/compare", json={"workloads": workloads}))

    assert done["type"] == "done"
    assert len(done["results"]) == width
    assert all(result["structured"] for result in done["results"])
    assert api.count == width
    assert done["usage"]["calls"] == width


@pytest.mark.parametrize("width", [2, 3, 4])
def test_each_column_of_a_comparison_streams_on_its_own(client, width):
    workloads = [f"Option {n}" for n in range(width)]
    events = sse_events(client.post("/api/compare", json={"workloads": workloads}))
    progress = [event for event in events if event["type"] in ("thinking", "partial")]

    assert progress
    # Every progress event says which column it belongs to, and every column
    # reports: an index clamped to the first two would lose the rest.
    assert {event["index"] for event in progress} == set(range(width))


@pytest.mark.parametrize(
    "workloads",
    [
        ["only one"],
        [],
        ["a", ""],
        ["a", "b", "c", "d", "e"],
        ["x" * 25_000, "b"],
    ],
)
def test_a_comparison_that_cannot_run_is_refused(client, api, workloads):
    response = client.post("/api/compare", json={"workloads": workloads})
    assert response.status_code == 400
    assert api.count == 0


def test_a_wider_comparison_costs_more_of_the_allowance(limited):
    """F9: four architectures is four calls, and the limit has to charge for four."""
    allowed = 0
    for _ in range(10):
        response = limited.post("/api/compare", json={"workloads": ["a", "b", "c", "d"]})
        if response.status_code == 429:
            break
        sse_events(response)  # reading the stream is what runs it
        allowed += 1
    else:
        pytest.fail("the limiter never fired")

    # SESSION_LIMITS is 10 a minute, and each of these costs four.
    assert allowed == 2


def test_a_failure_on_either_side_fails_the_comparison(client, api):
    api.raise_with = AdvisorError("timed out", kind="timeout", usage={"calls": 1, "costUsd": 0.01})
    failure = final_event(client.post("/api/compare", json={"workloads": ["a", "b"]}))

    assert failure["type"] == "error"
    assert failure["kind"] == "timeout"
    # Both halves ran, so both are accounted for even though neither is shown.
    assert failure["usage"]["calls"] == 2


# --------------------------------------------------------------------------- #
# /api/save
# --------------------------------------------------------------------------- #


def test_saving_stores_the_conversation_and_reports_which_one(client):
    conversation_id = save_one(client)
    assert isinstance(conversation_id, int)
    assert store.read(conversation_id)["title"] == "NHS records"


def test_saving_names_a_conversation_that_was_not_named(client):
    """A5: the rule for naming one lives in the store, not in the browser."""
    response = client.post(
        "/api/save",
        json={
            "messages": [{"role": "user", "content": "A patient records system for an NHS trust"}]
        },
    )
    assert response.get_json()["title"] == "A patient records system for an…"


def test_a_saved_conversation_keeps_the_running_total(client):
    usage = {"calls": 3, "inputTokens": 10, "outputTokens": 20, "costUsd": 0.5, "priced": True}
    saved = store.read(save_one(client, usage=usage))

    assert saved["usage"]["calls"] == 3
    assert saved["usage"]["costUsd"] == 0.5


def test_a_usage_total_that_is_not_numbers_is_dropped(client):
    assert store.read(save_one(client, usage={"calls": "lots", "costUsd": None}))["usage"] is None


def test_a_long_title_is_cut_to_fit(client):
    assert len(store.read(save_one(client, title="T" * 200))["title"]) == store.MAX_TITLE


def test_saving_nothing_is_refused(client):
    response = client.post("/api/save", json={"messages": []})
    assert response.status_code == 400
    assert "nothing to save" in response.get_json()["error"]


def test_saving_something_oversized_is_refused(client, conversations):
    response = client.post(
        "/api/save", json={"messages": [{"role": "user", "content": "x" * 25_000}]}
    )
    assert response.status_code == 400
    assert response.get_json()["kind"] == "too_long"
    assert list(conversations.glob("*.json")) == []


def test_a_comparison_is_saved_as_one_conversation(client):
    response = client.post(
        "/api/save",
        json={
            "messages": [
                {"role": "user", "content": "A"},
                {"role": "assistant", "content": FULL_JSON},
                {"role": "user", "content": "B"},
                {"role": "assistant", "content": FULL_JSON},
            ],
            "mode": "compare",
            "title": "Compare: A vs B",
        },
    )
    conversation_id = response.get_json()["id"]
    opened = client.get(f"/api/conversations/{conversation_id}").get_json()

    assert opened["mode"] == "compare"
    assert len(opened["messages"]) == 4
    assert opened["title"] == "Compare: A vs B"


def test_an_unknown_mode_falls_back_to_advise(client):
    assert store.read(save_one(client, mode="something else"))["mode"] == "advise"


def test_a_failure_to_write_is_reported(client, monkeypatch):
    def explode(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(server.store, "save", explode)
    response = client.post("/api/save", json={"messages": [{"role": "user", "content": "A"}]})
    assert response.status_code == 500
    assert "Could not save" in response.get_json()["error"]


# --------------------------------------------------------------------------- #
# Listing, reading, renaming and deleting
# --------------------------------------------------------------------------- #


def test_an_empty_store_lists_nothing(client):
    assert client.get("/api/conversations").get_json()["conversations"] == []


def test_a_listed_conversation_describes_itself(client):
    save_one(client, title=None)
    item = client.get("/api/conversations").get_json()["conversations"][0]

    assert item["titled"] is False
    # The fallback title is the first question, cut on a word boundary.
    assert item["title"] == "A patient records system for an…"
    assert item["messageCount"] == 2
    assert item["mode"] == "advise"
    assert item["model"] == advisor.MODEL
    assert item["savedAt"]


def test_the_newest_conversation_comes_first(client):
    ids = [save_one(client, title=f"C{n}") for n in range(3)]
    listed = [item["id"] for item in client.get("/api/conversations").get_json()["conversations"]]
    assert listed == list(reversed(ids))


def test_reading_a_conversation_rebuilds_it_as_it_first_rendered(client):
    conversation_id = save_one(client)
    opened = client.get(f"/api/conversations/{conversation_id}").get_json()

    assert [message["role"] for message in opened["messages"]] == ["user", "assistant"]
    assert opened["messages"][1]["structured"] is True
    # Saving trims the surrounding whitespace off each message; nothing else.
    assert opened["messages"][1]["raw"] == FULL_JSON.strip()
    assert opened["messages"][1]["diagram"]["nodes"]
    assert opened["title"] == "NHS records"


def test_a_conversation_saved_before_the_schema_still_opens(client):
    """R4: an unmigrated conversation reads as prose rather than failing to open."""
    conversation_id = save_one(client, title="Old one", reply=LEGACY_REPLY)
    reply = client.get(f"/api/conversations/{conversation_id}").get_json()["messages"][1]

    assert reply["structured"] is False
    assert "Recommended Services" in reply["prose"]
    # The Mermaid block is still pulled out and drawn.
    assert reply["diagram"]["nodes"]


@pytest.mark.parametrize(
    "content",
    ["{ not json", '{"headline": "no services"}', "[]", "  ", "plain prose"],
)
def test_a_reply_that_is_not_a_recommendation_reads_as_prose(content):
    assert server._as_recommendation(content) is None


def test_reading_returns_the_stored_cost_or_none(client):
    with_usage = save_one(client, usage={"calls": 2, "costUsd": 0.03, "priced": True})
    assert client.get(f"/api/conversations/{with_usage}").get_json()["usage"]["calls"] == 2

    without = save_one(client, title="No usage")
    assert client.get(f"/api/conversations/{without}").get_json()["usage"] is None


@pytest.mark.parametrize("name", ["999", "0", "-1", "../../secret.json", "abc", ""])
def test_only_a_conversation_that_exists_can_be_read(client, name):
    """An integer id is not a path, so there is nothing left to traverse."""
    assert client.get(f"/api/conversations/{name}").status_code == 404


def test_a_conversation_can_be_downloaded_as_json(client):
    """A1: storage moved to SQLite; the portable format did not have to."""
    conversation_id = save_one(client)
    response = client.get(f"/api/conversations/{conversation_id}/export")

    assert response.status_code == 200
    assert "attachment" in response.headers["Content-Disposition"]
    assert ".json" in response.headers["Content-Disposition"]

    payload = response.get_json()
    assert payload["mode"] == "advise"
    assert payload["title"] == "NHS records"
    assert payload["messages"][1]["content"] == FULL_JSON
    assert payload["version"] == store.SAVE_VERSION


def test_exporting_something_that_is_not_there(client):
    assert client.get("/api/conversations/999/export").status_code == 404


def test_renaming_changes_the_title(client):
    conversation_id = save_one(client)
    response = client.post(f"/api/conversations/{conversation_id}/title", json={"title": "Renamed"})

    assert response.status_code == 200
    assert response.get_json()["title"] == "Renamed"

    item = client.get("/api/conversations").get_json()["conversations"][0]
    assert item["title"] == "Renamed"
    assert item["titled"] is True


def test_a_blank_rename_is_refused(client):
    conversation_id = save_one(client)
    response = client.post(f"/api/conversations/{conversation_id}/title", json={"title": "   "})
    assert response.status_code == 400
    assert response.get_json()["kind"] == "empty"


def test_renaming_something_that_is_not_there(client):
    assert client.post("/api/conversations/999/title", json={"title": "x"}).status_code == 404


def test_deleting_removes_the_conversation_once(client):
    conversation_id = save_one(client)
    response = client.delete(f"/api/conversations/{conversation_id}")

    assert response.status_code == 200
    assert response.get_json()["deleted"] is True
    assert client.delete(f"/api/conversations/{conversation_id}").status_code == 404
    assert client.get("/api/conversations").get_json()["conversations"] == []


# --------------------------------------------------------------------------- #
# Spend guards (S4)
# --------------------------------------------------------------------------- #


def test_a_runaway_tab_is_cut_off_before_it_spends_the_month(limited):
    """A loop in an open browser tab is the case this exists for."""
    allowed = 0
    for _ in range(30):
        response = advise(limited, [{"role": "user", "content": "A shop"}])
        if response.status_code == 429:
            break
        allowed += 1
    else:
        pytest.fail("the limiter never fired")

    assert allowed == 10  # SESSION_LIMITS, per minute
    body = response.get_json()
    assert body["kind"] == "rate_limit"
    assert body["retryAfter"] == 60
    assert "more requests than this tool expects" in body["error"]
    # The rule, not the limiter's own repr of it, which runs to a paragraph.
    assert "10 per 1 minute" in body["error"]
    assert "key_func" not in body["error"]


def test_a_comparison_counts_double(limited):
    """It is two calls, so it costs two against the limit."""
    allowed = 0
    for _ in range(30):
        response = limited.post("/api/compare", json={"workloads": ["a", "b"]})
        if response.status_code == 429:
            break
        sse_events(response)
        allowed += 1

    assert allowed == 5


def test_reading_and_saving_are_not_rate_limited(limited):
    """Only the endpoints that spend money are capped."""
    for _ in range(40):
        assert limited.get("/api/conversations").status_code == 200
        assert limited.get("/api/health").status_code == 200


def test_the_daily_ceiling_refuses_before_the_api_does(client, api, monkeypatch):
    monkeypatch.setattr(server, "DAILY_TOKEN_CEILING", 2_000)

    # Well under: the call goes through, and is counted.
    assert final_event(advise(client, [{"role": "user", "content": "A"}]))["type"] == "done"
    assert store.spent_on()["tokens"] == 1_500

    store.record_call({"calls": 1, "inputTokens": 1_000}, "follow_up")

    response = advise(client, [{"role": "user", "content": "A"}])
    assert response.status_code == 429
    body = response.get_json()
    assert body["kind"] == "rate_limit"
    assert "2,000 tokens is used up" in body["error"]
    assert "midnight" in body["error"]
    # The call never reached the API.
    assert api.count == 1


def test_the_ceiling_covers_comparisons_too(client, api, monkeypatch):
    monkeypatch.setattr(server, "DAILY_TOKEN_CEILING", 100)
    store.record_call({"calls": 1, "inputTokens": 500}, "recommendation")

    response = client.post("/api/compare", json={"workloads": ["a", "b"]})
    assert response.status_code == 429
    assert api.count == 0


def test_the_ceiling_can_be_turned_off(client, api, monkeypatch):
    monkeypatch.setattr(server, "DAILY_TOKEN_CEILING", 0)
    store.record_call({"calls": 1, "inputTokens": 10_000_000}, "recommendation")

    assert final_event(advise(client, [{"role": "user", "content": "A"}]))["type"] == "done"
    assert api.count == 1


def test_health_reports_what_is_left_of_the_budget(client):
    store.record_call({"calls": 1, "inputTokens": 1_000, "outputTokens": 500}, "recommendation")
    budget = client.get("/api/health").get_json()["budget"]

    assert budget["ceiling"] == server.DAILY_TOKEN_CEILING
    assert budget["tokens"] == 1_500
    assert budget["costUsd"] >= 0


def test_each_tab_gets_its_own_allowance(limited):
    """A session id in a cookie, so one tab's loop does not lock out another."""
    for _ in range(10):
        advise(limited, [{"role": "user", "content": "A shop"}])
    assert advise(limited, [{"role": "user", "content": "A shop"}]).status_code == 429

    # A different cookie jar is a different tab, and starts again -- until the
    # per-IP limit, which is what catches a machine rather than a tab.
    with server.app.test_client() as other_tab:
        assert advise(other_tab, [{"role": "user", "content": "A shop"}]).status_code == 200


def test_one_machine_cannot_get_round_it_by_dropping_its_cookie(limited):
    refused = 0
    for _ in range(40):
        with server.app.test_client() as fresh_tab:
            if advise(fresh_tab, [{"role": "user", "content": "A shop"}]).status_code == 429:
                refused += 1

    # IP_LIMITS is 30 a minute, so the last ten are refused however many cookies
    # the client throws away.
    assert refused == 10


# --------------------------------------------------------------------------- #
# The module itself
# --------------------------------------------------------------------------- #


def test_every_route_is_registered_before_the_server_starts():
    """`python server.py` runs the file top to bottom, and app.run() never returns.

    A route decorated below the __main__ block is therefore registered when the
    module is imported -- which is what the tests here do -- and not when the app
    is actually served: the URL falls through to Flask's static catch-all, which
    is GET-only, and a POST to it comes back 405. This suite cannot see that
    difference, so it checks the source order instead.
    """
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    main_at = source.index('if __name__ == "__main__":')

    below = [line.strip() for line in source[main_at:].splitlines() if line.startswith("@app.")]
    assert below == [], f"registered after the server starts: {below}"


def test_the_routes_the_front_end_calls_all_exist():
    """A cheap guard on the URL map, which is the contract app.js codes against."""
    rules = {
        (rule.rule, method)
        for rule in server.app.url_map.iter_rules()
        for method in rule.methods or ()
    }
    for path, method in (
        ("/api/health", "GET"),
        ("/api/advise", "POST"),
        ("/api/compare", "POST"),
        ("/api/estimate", "POST"),
        ("/api/export", "POST"),
        ("/api/save", "POST"),
        ("/api/conversations", "GET"),
    ):
        assert (path, method) in rules, f"{method} {path} is not routed"


# --------------------------------------------------------------------------- #
# Revising the architecture
#
# A thread used to hold exactly one architecture: the first reply was structured
# and every later turn was prose, so a conversation that changed the sizes,
# swapped an engine or dropped a service left the deliverable describing the
# opening guess. A revision is a second structured turn, asked for explicitly,
# and everything downstream picks it up because it is the same Recommendation.
# --------------------------------------------------------------------------- #


def thread_with_follow_up(question: str = "Make it two Graviton servers on Postgres."):
    """A recommendation, then a question about it, ending on a user turn."""
    return [
        {"role": "user", "content": "An online shop that falls over on Black Friday"},
        {"role": "assistant", "content": FULL_JSON},
        {"role": "user", "content": question},
    ]


def test_a_revision_is_asked_for_rather_than_guessed_at(client, api):
    """Nothing reads the user's words to decide. The flag decides."""
    client.post("/api/advise", json={"messages": thread_with_follow_up()})
    assert api.calls[-1]["kind"] == "follow_up"

    client.post("/api/advise", json={"messages": thread_with_follow_up(), "revise": True})
    assert api.calls[-1]["kind"] == "revision"


def test_a_revision_comes_back_as_an_architecture_not_as_prose(client, api):
    response = client.post(
        "/api/advise", json={"messages": thread_with_follow_up(), "revise": True}
    )
    assert response.status_code == 200

    done = [event for event in sse_events(response) if event["type"] == "done"]
    assert len(done) == 1
    message = done[0]["message"]
    assert message["structured"] is True
    assert message["headline"] == "Two Graviton servers, Postgres, and no read replica"


def test_a_half_written_revision_is_drawn_as_an_architecture(client, api):
    """The frame used to key off "is this the first turn", which a revision is not.

    Without this the schema streams at the user as prose, a field at a time.
    """
    response = client.post(
        "/api/advise", json={"messages": thread_with_follow_up(), "revise": True}
    )
    partials = [event for event in sse_events(response) if event["type"] == "partial"]

    assert partials, "a streamed reply reported no progress at all"
    assert all(event["message"]["structured"] is True for event in partials)


def test_there_is_nothing_to_revise_on_the_first_turn(client, api):
    """The flag is ignored rather than honoured into a nonsense turn."""
    response = client.post(
        "/api/advise",
        json={"messages": [{"role": "user", "content": "A shop"}], "revise": True},
    )
    assert response.status_code == 200
    assert api.calls[-1]["kind"] == "recommendation"


def test_a_revision_is_the_architecture_a_document_describes(client):
    """The end of it: what a client receives is what the conversation settled on."""
    _, names = exported(
        client,
        formats=["md"],
        messages=[
            {"role": "user", "content": "An online shop that falls over on Black Friday"},
            {"role": "assistant", "content": FULL_JSON},
            {"role": "user", "content": "Two Graviton servers on Postgres, and drop the replica."},
            {"role": "assistant", "content": REVISED_JSON},
        ],
    )
    assert "report.md" in names

    response = client.post(
        "/api/export",
        json=export_body(
            formats=["md"],
            messages=[
                {"role": "user", "content": "An online shop that falls over on Black Friday"},
                {"role": "assistant", "content": FULL_JSON},
                {"role": "user", "content": "Two Graviton servers on Postgres."},
                {"role": "assistant", "content": REVISED_JSON},
            ],
        ),
    )
    with zipfile.ZipFile(io.BytesIO(response.get_data())) as archive:
        report = archive.read(
            next(name for name in archive.namelist() if name.endswith("report.md"))
        ).decode("utf-8")

    # The appendices are the record of the conversation and should mention the
    # architecture that was replaced. The document is what the client is being
    # handed, and it describes one architecture: the current one.
    body = report.split("## Appendix")[0]

    assert "Two Graviton servers, Postgres, and no read replica" in body
    assert "RDS PostgreSQL (Multi-AZ)" in body
    assert "RDS Read Replica" not in body
    assert "Spread the load, cache the reads" not in body
    # And the transcript still says an earlier architecture was given.
    assert "Recommended an architecture: Spread the load" in report


def test_a_revision_reaches_the_terraform_module_too(client):
    """F4's whole complaint: an opening brief has no specifics in it."""
    response = client.post(
        "/api/export",
        json=export_body(
            formats=["tf"],
            messages=[
                {"role": "user", "content": "An online shop that falls over on Black Friday"},
                {"role": "assistant", "content": FULL_JSON},
                {"role": "user", "content": "Two Graviton servers on Postgres."},
                {"role": "assistant", "content": REVISED_JSON},
            ],
        ),
    )
    assert response.status_code == 200

    with zipfile.ZipFile(io.BytesIO(response.get_data())) as archive:
        main = archive.read(
            next(name for name in archive.namelist() if name.endswith("main.tf"))
        ).decode("utf-8")

    assert '"m7g.large"' in main
    assert '"db.r6g.xlarge"' in main
    assert '"postgres"' in main
    # And the arm64 image the revised instance type needs, not the x86 one.
    assert "al2023_arm64" in main
    for gone in ('"m6i.large"', '"db.m6g.large"', '"mysql"', "replicate_source_db"):
        assert gone not in main, gone


def test_the_document_carries_no_revision_count(client):
    """A client document is the architecture, not its history.

    "Revision 3 of 3" on a page invites questions about the two revisions the
    client never saw. Nothing in the deliverable says which one this is.
    """
    response = client.post(
        "/api/export",
        json=export_body(
            formats=["md"],
            messages=[
                {"role": "user", "content": "An online shop"},
                {"role": "assistant", "content": FULL_JSON},
                {"role": "user", "content": "Revise it."},
                {"role": "assistant", "content": REVISED_JSON},
                {"role": "user", "content": "Revise it again."},
                {"role": "assistant", "content": REVISED_JSON},
            ],
        ),
    )
    with zipfile.ZipFile(io.BytesIO(response.get_data())) as archive:
        report = archive.read(
            next(name for name in archive.namelist() if name.endswith("report.md"))
        ).decode("utf-8")

    body = report.split("## Appendix")[0]
    for absent in ("revision", "Revision", "revised at", "of 3", "of 2"):
        assert absent not in body, absent


# --------------------------------------------------------------------------- #
# /api/export (F3)
# --------------------------------------------------------------------------- #

# The formats that need no browser, so these tests never print a PDF.
TEXT_FORMATS = ["md", "html"]


def export_body(reply=FULL_JSON, **extra):
    body = {
        "messages": [
            {"role": "user", "content": "An online shop that falls over on Black Friday"},
            {"role": "assistant", "content": reply},
        ],
        "mode": "advise",
        "formats": TEXT_FORMATS,
        "client": "Northbridge Mutual",
        "preparedBy": "A. Consultant, Insert Company Name",
    }
    body.update(extra)
    return body


def exported(client, **extra):
    """Post an export and hand back (response, the names inside the zip)."""
    response = client.post("/api/export", json=export_body(**extra))
    assert response.status_code == 200, response.get_json()
    with zipfile.ZipFile(io.BytesIO(response.get_data())) as archive:
        names = [name.split("/", 1)[-1] for name in archive.namelist()]
    return response, names


def test_an_export_comes_back_as_a_named_download(client):
    response, names = exported(client)

    today = datetime.now(store.UK).date().isoformat()
    assert response.mimetype == "application/zip"
    assert response.headers["Content-Disposition"] == (
        f'attachment; filename="architecture-review-northbridge-mutual-{today}.zip"'
    )
    assert response.headers["Cache-Control"] == "no-store"
    assert sorted(names) == sorted(["report.md", "report.html", "diagram.svg"])


def test_one_self_contained_format_comes_back_as_that_file(client):
    response = client.post("/api/export", json=export_body(formats=["html"]))

    assert response.status_code == 200
    assert response.mimetype == "text/html"
    assert response.headers["Content-Disposition"].endswith('.html"')
    assert b"<!DOCTYPE html>" in response.get_data()


def test_the_document_carries_what_the_dialog_was_told(client):
    _, _ = exported(client)
    response = client.post(
        "/api/export",
        json=export_body(formats=["md"], client="Leeds Trust", preparedBy="A Reviewer"),
    )
    with zipfile.ZipFile(io.BytesIO(response.get_data())) as archive:
        text = archive.read(
            next(name for name in archive.namelist() if name.endswith("report.md"))
        ).decode("utf-8")

    assert "client: Leeds Trust" in text
    assert "prepared_by: A Reviewer" in text
    assert "leeds-trust" in response.headers["Content-Disposition"]


def test_a_cached_estimate_is_priced_into_the_document(client):
    estimate = {
        "priced": True,
        "region": "eu-west-2",
        "monthlyUsd": 1234.56,
        "tier": "Medium",
        "pricedAt": "2026-08-19T12:00:00+01:00",
        "unpricedServices": 0,
        "services": [
            {
                "name": "RDS MySQL (Multi-AZ)",
                "monthlyUsd": 1234.56,
                "priced": True,
                "lines": [
                    {
                        "meter": "rds-instance-multi-az",
                        "priced": True,
                        "monthlyUsd": 1234.56,
                        "detail": "1 x $1.69/hour x 730h",
                    }
                ],
            }
        ],
    }
    response = client.post("/api/export", json=export_body(formats=["md"], estimates=[estimate]))
    with zipfile.ZipFile(io.BytesIO(response.get_data())) as archive:
        text = archive.read(
            next(name for name in archive.namelist() if name.endswith("report.md"))
        ).decode("utf-8")

    assert "$1,234.56" in text
    assert "1 x $1.69/hour x 730h" in text


def test_rubbish_where_an_estimate_should_be_is_dropped(client):
    """The figures come back through a request body, so nothing is taken on trust."""
    for estimate in ("not an estimate", 12, {"priced": True}, {"priced": True, "services": "x"}):
        response = client.post(
            "/api/export", json=export_body(formats=["md"], estimates=[estimate])
        )
        assert response.status_code == 200
        with zipfile.ZipFile(io.BytesIO(response.get_data())) as archive:
            text = archive.read(
                next(name for name in archive.namelist() if name.endswith("report.md"))
            ).decode("utf-8")
        assert "no priced estimate" in text


def test_a_figure_that_is_not_a_number_prices_as_nothing(client):
    """A total of "lots" is worth nothing, not a crash and not a made-up number."""
    estimate = {
        "priced": True,
        "monthlyUsd": "lots",
        "services": [{"name": "S3", "monthlyUsd": None, "priced": True, "lines": []}],
    }
    response = client.post("/api/export", json=export_body(formats=["md"], estimates=[estimate]))
    assert response.status_code == 200

    with zipfile.ZipFile(io.BytesIO(response.get_data())) as archive:
        text = archive.read(
            next(name for name in archive.namelist() if name.endswith("report.md"))
        ).decode("utf-8")
    # A total that coerces to nothing is no figure at all, not a $0.00 table.
    # Printing zero would be the made-up number this guards against.
    assert "**Total**" not in text
    assert "monthly_usd:" not in text
    assert "no priced estimate" in text


def test_a_conversation_with_no_architecture_cannot_be_exported(client):
    response = client.post(
        "/api/export",
        json={"messages": [{"role": "user", "content": "hello"}], "formats": ["md"]},
    )
    assert response.status_code == 400
    assert response.get_json()["kind"] == "empty"
    assert "no reviewed architecture" in response.get_json()["error"]


def test_a_prose_reply_is_not_mistaken_for_an_architecture(client):
    response = client.post("/api/export", json=export_body(reply="Just some prose."))
    assert response.status_code == 400


def test_the_terraform_module_comes_back_with_the_report(client):
    """F4 over the wire: one request, a document and a module beside it."""
    _, names = exported(client, formats=["md", "tf"])

    assert "report.md" in names
    assert "terraform/main.tf" in names
    assert "terraform/README.md" in names


def test_the_terraform_module_is_written_for_the_client_the_export_names(client):
    response = client.post(
        "/api/export", json=export_body(formats=["tf"], client="Leeds Teaching Hospitals")
    )
    assert response.status_code == 200

    with zipfile.ZipFile(io.BytesIO(response.get_data())) as archive:
        variables = archive.read(
            next(name for name in archive.namelist() if name.endswith("variables.tf"))
        ).decode("utf-8")

    assert 'default     = "Leeds Teaching Hospitals"' in variables
    # Shortened at a word rather than mid-word: 'leeds', not 'leeds-teachi'.
    assert 'default     = "leeds"' in variables


def test_a_comparison_exports_a_module_an_option(client):
    other = json.loads(FULL_JSON)
    other["headline"] = "Serverless, and pay per request"
    response = client.post(
        "/api/export",
        json={
            "messages": [
                {"role": "user", "content": "The shop, as it stands"},
                {"role": "assistant", "content": FULL_JSON},
                {"role": "user", "content": "The shop, rebuilt serverless"},
                {"role": "assistant", "content": json.dumps(other)},
            ],
            "mode": "compare",
            "formats": ["tf"],
            "client": "Northbridge Mutual",
        },
    )
    assert response.status_code == 200

    with zipfile.ZipFile(io.BytesIO(response.get_data())) as archive:
        names = [name.split("/", 1)[-1] for name in archive.namelist()]

    assert "terraform-option-a/main.tf" in names
    assert "terraform-option-b/main.tf" in names


@pytest.mark.parametrize("formats", [[], ["docx"], "md", None])
def test_a_format_the_app_does_not_write_is_refused(client, formats):
    response = client.post("/api/export", json=export_body(formats=formats))
    assert response.status_code == 400
    assert "format" in response.get_json()["error"]


def test_an_oversized_conversation_is_refused_before_it_is_written(client):
    response = client.post(
        "/api/export",
        json=export_body(
            messages=[
                {"role": "user", "content": "x" * 25_000},
                {"role": "assistant", "content": FULL_JSON},
            ]
        ),
    )
    assert response.status_code == 400
    assert response.get_json()["kind"] == "too_long"


def test_a_comparison_exports_both_options_as_one_document(client):
    other = json.loads(FULL_JSON)
    other["headline"] = "Serverless, and pay per request"
    response = client.post(
        "/api/export",
        json={
            "messages": [
                {"role": "user", "content": "The shop, as it stands"},
                {"role": "assistant", "content": FULL_JSON},
                {"role": "user", "content": "The shop, rebuilt serverless"},
                {"role": "assistant", "content": json.dumps(other)},
            ],
            "mode": "compare",
            "formats": ["md"],
            "client": "Northbridge Mutual",
        },
    )
    assert response.status_code == 200
    assert "architecture-options-northbridge-mutual" in response.headers["Content-Disposition"]

    with zipfile.ZipFile(io.BytesIO(response.get_data())) as archive:
        text = archive.read(
            next(name for name in archive.namelist() if name.endswith("report.md"))
        ).decode("utf-8")
    assert "## 1. Side by side" in text
    assert "Serverless, and pay per request" in text
    assert "Option A" in text and "Option B" in text


def test_a_pdf_with_no_browser_still_sends_the_rest_and_says_why(client, monkeypatch):
    monkeypatch.setattr(server.export, "chrome", lambda: None)
    response = client.post("/api/export", json=export_body(formats=["pdf", "md"]))

    assert response.status_code == 200
    assert "No browser was found" in response.headers["X-Export-Note"]
    with zipfile.ZipFile(io.BytesIO(response.get_data())) as archive:
        assert not any(name.endswith(".pdf") for name in archive.namelist())


def test_a_pdf_alone_with_no_browser_is_an_error_with_a_reason(client, monkeypatch):
    monkeypatch.setattr(server.export, "chrome", lambda: None)
    response = client.post("/api/export", json=export_body(formats=["pdf"]))

    assert response.status_code == 502
    assert response.get_json()["kind"] == "no_browser"
    assert "open report.html and print it" in response.get_json()["error"]


def test_health_says_whether_a_pdf_can_be_printed(client, monkeypatch):
    monkeypatch.setattr(server.export, "chrome", lambda: None)
    assert client.get("/api/health").get_json()["exports"] == {"pdf": False}

    monkeypatch.setattr(server.export, "chrome", lambda: "/usr/bin/chromium")
    assert client.get("/api/health").get_json()["exports"] == {"pdf": True}


def test_the_session_file_is_the_document_the_store_exports(client):
    response = client.post("/api/export", json=export_body(formats=["md", "json"]))
    with zipfile.ZipFile(io.BytesIO(response.get_data())) as archive:
        session = json.loads(
            archive.read(next(n for n in archive.namelist() if n.endswith("session.json")))
        )

    assert set(session) == {"timestamp", "version", "model", "mode", "messages"}
    assert session["version"] == store.SAVE_VERSION
    assert session["mode"] == "advise"
    assert len(session["messages"]) == 2


def test_exporting_is_limited_on_its_own_allowance(limited):
    """An export starts a browser, so it is the most expensive thing here (S4)."""
    limit = int(server.EXPORT_LIMITS.split(" per ")[0])
    statuses = [
        limited.post("/api/export", json=export_body(formats=["md"])).status_code
        for _ in range(limit + 1)
    ]

    assert statuses.count(200) == limit
    assert statuses[-1] == 429


def test_a_conversation_saved_before_the_pricing_fields_still_exports(client):
    """F1 added `region` and per-service `usage`; everything saved earlier has neither.

    Validating strictly against today's schema refused those conversations with
    "there is no reviewed architecture to export", which is both wrong and
    unhelpful: neither field is printed in the document.
    """
    legacy = json.loads(FULL_JSON)
    legacy.pop("region")
    for service in legacy["services"]:
        service.pop("usage")

    response = client.post(
        "/api/export", json=export_body(reply=json.dumps(legacy), formats=["md"])
    )
    assert response.status_code == 200, response.get_json()

    with zipfile.ZipFile(io.BytesIO(response.get_data())) as archive:
        text = archive.read(
            next(name for name in archive.namelist() if name.endswith("report.md"))
        ).decode("utf-8")

    # It exports, and says nothing about a region nobody chose.
    assert "## 3. Services" in text
    assert "region:" not in text
    assert "eu-west-2" not in text


def test_a_reply_that_is_not_a_recommendation_at_all_is_still_skipped(client):
    """Tolerating missing fields must not mean accepting any JSON object."""
    response = client.post(
        "/api/export",
        json=export_body(reply=json.dumps({"headline": "x", "services": "not a list"})),
    )
    assert response.status_code == 400
    assert response.get_json()["kind"] == "empty"


def test_an_advisory_thread_documents_one_architecture(client):
    """A follow-up that revised the architecture replaced it; it is not Option B."""
    revised = json.loads(FULL_JSON)
    revised["headline"] = "Revised after the follow-up"

    response = client.post(
        "/api/export",
        json={
            "messages": [
                {"role": "user", "content": "The shop, as it stands"},
                {"role": "assistant", "content": FULL_JSON},
                {"role": "user", "content": "Make it cheaper"},
                {"role": "assistant", "content": json.dumps(revised)},
            ],
            "mode": "advise",
            "formats": ["md"],
        },
    )
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.get_data())) as archive:
        text = archive.read(
            next(name for name in archive.namelist() if name.endswith("report.md"))
        ).decode("utf-8")

    # The latest architecture, documented once, under the brief that opened the
    # thread rather than the question that revised it.
    assert "Revised after the follow-up" in text
    assert "Option A" not in text
    assert "Option B" not in text
    assert "The shop, as it stands" in text
    assert "architecture-review-" in response.headers["Content-Disposition"]


# --------------------------------------------------------------------------- #
# Region and compliance (F5)
# --------------------------------------------------------------------------- #


def test_a_region_and_a_regime_reach_the_call(client, api):
    ask_with = {
        "messages": [{"role": "user", "content": "A patient records system"}],
        "region": "eu-west-2",
        "compliance": "nhs-dspt",
    }
    sse_events(client.post("/api/advise", json=ask_with))

    constraints = api.calls[-1]["constraints"]
    assert "Build this in eu-west-2." in constraints
    assert "Data Security and Protection Toolkit" in constraints


def test_the_conversation_the_user_wrote_is_the_one_that_is_named(client, api):
    """The constraints ride on the brief, so they must not leak into the title."""
    response = client.post(
        "/api/advise",
        json={
            "messages": [{"role": "user", "content": "A patient records system"}],
            "region": "us-east-1",
        },
    )
    done = final_event(response)

    assert done["title"] == "A patient records system"
    assert api.calls[-1]["messages"][0]["content"] == "A patient records system"


def test_a_region_that_is_not_one_is_ignored_rather_than_refused(client, api):
    """These come from a select, so a bad value is a stale tab, not a request to fail."""
    response = client.post(
        "/api/advise",
        json={
            "messages": [{"role": "user", "content": "A shop"}],
            "region": "mars-central-1",
            "compliance": "iso-9001",
        },
    )
    assert final_event(response)["type"] == "done"
    assert api.calls[-1]["constraints"] == ""


def test_every_option_in_a_comparison_is_built_to_the_same_constraints(client, api):
    """Comparing a London architecture with an Oregon one is not a comparison."""
    sse_events(
        client.post(
            "/api/compare",
            json={"workloads": ["Serverless", "On EC2", "On ECS"], "region": "eu-west-1"},
        )
    )

    assert len(api.calls) == 3
    assert {call["constraints"] for call in api.calls} == {"Build this in eu-west-1."}


def test_the_regime_a_review_was_built_for_survives_being_reopened(client):
    conversation_id = save_one(client, compliance="pci-dss")
    reopened = client.get(f"/api/conversations/{conversation_id}").get_json()

    assert reopened["compliance"] == "pci-dss"


# --------------------------------------------------------------------------- #
# Finding one again (F8)
# --------------------------------------------------------------------------- #


def test_searching_finds_a_conversation_by_what_is_in_it(client):
    conversation_id = save_one(client)
    found = client.get("/api/search?q=patient records").get_json()

    assert [item["id"] for item in found["conversations"]] == [conversation_id]
    assert found["conversations"][0]["snippet"]
    assert client.get("/api/search?q=submarines").get_json()["conversations"] == []


def test_searching_by_facet_finds_it_too(client):
    conversation_id = save_one(client)

    for query in ("tier=Medium", "region=eu-west-2", "service=RDS"):
        found = client.get(f"/api/search?{query}").get_json()["conversations"]
        assert [item["id"] for item in found] == [conversation_id], query

    assert client.get("/api/search?tier=High").get_json()["conversations"] == []


def test_a_search_that_sqlite_cannot_run_is_a_bad_request_not_a_broken_page(client, monkeypatch):
    def explode(**_kwargs):
        raise sqlite3.OperationalError("malformed query")

    monkeypatch.setattr(store, "search", explode)
    response = client.get("/api/search?q=anything")

    assert response.status_code == 400
    assert response.get_json()["kind"] == "empty"


def test_tagging_and_untagging_a_conversation(client):
    conversation_id = save_one(client)

    added = client.post(f"/api/conversations/{conversation_id}/tags", json={"tag": "NHS"})
    assert added.status_code == 200
    assert added.get_json() == {"id": conversation_id, "tags": ["nhs"]}

    listed = client.get("/api/conversations").get_json()["conversations"]
    assert listed[0]["tags"] == ["nhs"]

    found = client.get("/api/search?tag=nhs").get_json()
    assert [item["id"] for item in found["conversations"]] == [conversation_id]
    assert found["tags"] == ["nhs"]

    removed = client.post(
        f"/api/conversations/{conversation_id}/tags", json={"tag": "nhs", "remove": True}
    )
    assert removed.get_json()["tags"] == []


def test_an_empty_tag_is_refused_and_a_missing_conversation_is_a_404(client):
    conversation_id = save_one(client)

    blank = client.post(f"/api/conversations/{conversation_id}/tags", json={"tag": "  "})
    assert blank.status_code == 400

    missing = client.post("/api/conversations/999/tags", json={"tag": "nhs"})
    assert missing.status_code == 404
    assert missing.get_json()["kind"] == "not_found"


def test_searching_and_tagging_are_not_rate_limited(limited):
    """They spend no Anthropic money, which is what the limits are for."""
    conversation_id = save_one(limited)
    for _ in range(40):
        assert limited.get("/api/search?q=patient").status_code == 200
        assert (
            limited.post(f"/api/conversations/{conversation_id}/tags", json={"tag": "nhs"})
        ).status_code == 200


# --------------------------------------------------------------------------- #
# The spend view over the ledger (F14)
# --------------------------------------------------------------------------- #


def test_the_spend_endpoint_reports_this_month(client):
    store.record_call(
        {"calls": 1, "inputTokens": 1000, "outputTokens": 500, "costUsd": 0.007}, "recommendation"
    )

    body = client.get("/api/usage").get_json()

    assert body["month"] == store.spend_report()["month"]
    assert [row["kind"] for row in body["kinds"]] == ["recommendation"]
    assert body["totals"]["calls"] == 1
    assert body["today"]["calls"] == 1
    assert body["days"][0]["kind"] == "recommendation"


def test_the_spend_endpoint_carries_the_ceiling(client):
    """The one place the daily ceiling is visible before it refuses something."""
    assert client.get("/api/usage").get_json()["ceiling"] == server.DAILY_TOKEN_CEILING


def test_the_spend_endpoint_reports_the_month_it_is_asked_for(client):
    assert client.get("/api/usage?month=2026-03").get_json()["month"] == "2026-03"


def test_a_month_the_spend_endpoint_cannot_read_is_a_bad_request(client):
    for asked in ("2026", "2026-13", "March", "'; DROP TABLE usage; --"):
        response = client.get(f"/api/usage?month={asked}")
        assert response.status_code == 400, asked
        assert response.get_json()["error"]


def test_reading_the_spend_costs_nothing(limited):
    """Unledgered and unlimited, like the rest of the reading endpoints."""
    for _ in range(40):
        assert limited.get("/api/usage").status_code == 200

    # Reading the ledger must not write to it.
    assert store.spent_on()["calls"] == 0


# --------------------------------------------------------------------------- #
# What stands in for a CSRF token (S3)
#
# There is no token and no authentication. What keeps a page on another origin
# from making this one delete a conversation or spend money is that every
# mutating endpoint reads its body with request.get_json, which needs a JSON
# content type -- and a cross-origin request carrying one needs a preflight this
# server never answers. SameSite=Lax closes the rest.
#
# That is a decision, and it holds only while the content type stays required.
# These are the tests that say so: a get_json(force=True) anywhere, or an
# endpoint that reads request.form, turns them red.
# --------------------------------------------------------------------------- #

MUTATING = (
    ("/api/advise", "post"),
    ("/api/compare", "post"),
    ("/api/estimate", "post"),
    ("/api/save", "post"),
    ("/api/export", "post"),
    ("/api/conversations/1/title", "post"),
    ("/api/conversations/1/tags", "post"),
)


@pytest.mark.parametrize(("path", "method"), MUTATING)
def test_a_form_encoded_post_changes_nothing(client, path, method):
    """The shape a cross-site form can actually send, refused on every endpoint.

    A form can only send urlencoded, multipart or text/plain -- never JSON
    without a preflight -- so this is the request a hostile page would make.
    """
    response = getattr(client, method)(
        path,
        data={"messages": "[]", "title": "hijacked", "tag": "hijacked", "raw": "{}"},
        content_type="application/x-www-form-urlencoded",
    )

    assert response.status_code >= 400
    assert response.status_code != 500


@pytest.mark.parametrize(("path", "method"), MUTATING)
def test_a_text_plain_post_changes_nothing(client, path, method):
    """The other content type a form can set without a preflight."""
    response = getattr(client, method)(
        path, data=json.dumps({"title": "hijacked"}), content_type="text/plain"
    )

    assert response.status_code >= 400
    assert response.status_code != 500


def test_a_form_post_cannot_rename_a_conversation(client):
    """The one that would actually be worth doing, checked end to end."""
    conversation_id = save_one(client, title="Original")

    response = client.post(
        f"/api/conversations/{conversation_id}/title",
        data={"title": "hijacked"},
        content_type="application/x-www-form-urlencoded",
    )

    assert response.status_code >= 400
    assert store.read(conversation_id)["title"] == "Original"


def test_a_form_post_cannot_tag_a_conversation(client):
    conversation_id = save_one(client)

    response = client.post(
        f"/api/conversations/{conversation_id}/tags",
        data={"tag": "hijacked"},
        content_type="application/x-www-form-urlencoded",
    )

    assert response.status_code >= 400
    assert store.tags_for(conversation_id) == []


def test_the_session_cookie_is_not_sent_across_sites(client):
    """The other half of it, and the half a browser enforces."""
    client.get("/api/health")

    assert server.app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert server.app.config["SESSION_COOKIE_HTTPONLY"] is True


def test_no_endpoint_forces_json_past_the_content_type():
    """force=True would read a form body as JSON and give the whole thing up.

    Both of these are named in _clean_messages' own docstring as the things not
    to do, so the backticked prose comes out before looking for the call.
    """
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    code = source.replace("`get_json(force=True)`", "").replace("`request.form`", "")

    assert "force=True" not in code
    assert "request.form" not in code
