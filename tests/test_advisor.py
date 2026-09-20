"""advisor.py: the client, the request, streaming, stop reasons and cost accounting."""

import json
import re
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from anthropic import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError
from anthropic.types import TextBlock
from pydantic import ValidationError

import advisor
from advisor import (
    AdvisorError,
    Progress,
    advise,
    call_claude,
    count_input_tokens,
    extract_mermaid,
    get_client,
    merge_usage,
    rates,
    usage_of,
)
from schema import Compliance, Recommendation
from tests.conftest import FAKE_KEY, FakeClient, cited, fake_response
from tests.samples import FULL_JSON, SERVICE_COUNT

ROOT = Path(__file__).resolve().parent.parent


def rate_limited(retry_after: str | None = "30") -> RateLimitError:
    headers = {"retry-after": retry_after} if retry_after else {}
    return RateLimitError(
        "rate limited",
        response=SimpleNamespace(status_code=429, headers=headers, request=None),
        body=None,
    )


# --------------------------------------------------------------------------- #
# The client
# --------------------------------------------------------------------------- #


def test_one_client_is_reused_per_key():
    assert get_client() is get_client()
    assert get_client("another-key") is not get_client()


def test_a_missing_key_is_a_user_facing_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    # get_client() re-reads .env before giving up, and this machine has a real one.
    monkeypatch.setattr(advisor, "load_dotenv", lambda *a, **k: False)

    with pytest.raises(AdvisorError) as caught:
        get_client()
    assert caught.value.kind == "missing_key"
    assert "ANTHROPIC_API_KEY" in str(caught.value)


def test_an_explicit_key_beats_the_environment(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setattr(advisor, "load_dotenv", lambda *a, **k: False)
    assert get_client("passed-in").api_key == "passed-in"


def test_the_client_retries_more_than_the_sdk_default():
    """R7: two attempts is the SDK default; a person waiting will take four."""
    assert get_client().max_retries == advisor.MAX_RETRIES > 2


# --------------------------------------------------------------------------- #
# The system prompt (R6)
# --------------------------------------------------------------------------- #


KINDS = ("recommendation", "comparison", "follow_up", "revision")


def test_the_cacheable_head_of_the_prompt_is_the_same_on_every_turn():
    """Caching is a prefix match, so what every turn shares has to come first.

    This replaces a test that asserted the whole prompt was identical across
    kinds. It was not a free property: a prose turn told to fill in a schema will
    do it, and answer a question about instance sizes with several thousand
    characters of JSON. What must not vary is the head, which is what a cache hit
    depends on; the tail says what this turn is.
    """
    for kind in KINDS:
        assert advisor.system_prompt(kind).startswith(advisor.SYSTEM_PROMPT)


def test_the_prompt_does_not_vary_within_a_kind(structured_client):
    call_claude(structured_client, [{"role": "user", "content": "hi"}], "recommendation")
    call_claude(structured_client, [{"role": "user", "content": "hi"}], "recommendation")

    prompts = {sent["system"] for sent in structured_client.messages.created}
    assert len(prompts) == 1


def test_a_prose_turn_is_told_something_a_structured_turn_is_not(structured_client):
    """Splitting them costs no cache: only a follow-up sends a tool, and `tools`
    is rendered before `system`, so the two never shared a prefix anyway."""
    call_claude(structured_client, [{"role": "user", "content": "hi"}], "recommendation")
    call_claude(structured_client, [{"role": "user", "content": "hi"}], "follow_up")

    first, second = (sent["system"] for sent in structured_client.messages.created)
    assert first != second


def test_a_prose_turn_is_forbidden_from_answering_with_the_schema():
    """The bug this split exists to fix, asserted so it cannot come back.

    Asked to "recommend specific sizes and resources", a follow-up decided it had
    been asked for an architecture and returned the whole schema as raw JSON into
    a chat bubble.
    """
    follow_up = advisor.INSTRUCTION_BY_KIND["follow_up"]

    assert "Never answer this turn with JSON" in follow_up
    assert "leave the architecture where it is" in follow_up
    # And it is told what to do instead, not only what not to do.
    assert "give exactly those specifics" in follow_up

    for structured in ("recommendation", "comparison", "revision"):
        assert (
            "constrained to a JSON schema" in advisor.INSTRUCTION_BY_KIND[structured]
            or "restate it in full against the schema" in advisor.INSTRUCTION_BY_KIND[structured]
        )


def test_nothing_is_interpolated_into_the_system_prompt():
    """A date, a username or a session id in here would void the cache every turn."""
    for kind in KINDS:
        text = advisor.system_prompt(kind)
        assert "{" not in text and "}" not in text


def test_a_comparison_is_not_told_there_are_two_of_them():
    """F9: a comparison can be three or four now, and the count must not be in the prompt.

    Interpolating the real count would be the obvious fix and the wrong one: it
    would give every width its own cache prefix.
    """
    text = advisor.INSTRUCTION_BY_KIND["comparison"]
    assert "two architectures" not in text
    assert "the other one" not in text
    assert "several architectures" in text


# --------------------------------------------------------------------------- #
# Region and compliance (F5)
# --------------------------------------------------------------------------- #


def test_an_unconstrained_brief_is_left_exactly_as_it_was():
    assert advisor.constraints_line() == ""
    messages = [{"role": "user", "content": "A shop"}]
    assert advisor.with_constraints(messages, "") == messages


def test_a_region_and_a_regime_both_reach_the_brief():
    line = advisor.constraints_line("eu-west-2", Compliance.NHS_DSPT)
    assert "Build this in eu-west-2." in line
    assert "Data Security and Protection Toolkit" in line


def test_no_regime_named_adds_nothing_about_one():
    line = advisor.constraints_line("us-east-1", Compliance.NONE)
    assert line == "Build this in us-east-1."


def test_the_constraints_go_in_the_messages_and_never_in_the_system_prompt(structured_client):
    """F5's whole design constraint: the cache prefix has to stay byte-stable."""
    call_claude(
        structured_client,
        [{"role": "user", "content": "A patient records system"}],
        "recommendation",
        constraints="Build this in eu-west-2.",
    )
    call_claude(
        structured_client,
        [{"role": "user", "content": "A patient records system"}],
        "recommendation",
        constraints="Build this in us-east-1.",
    )

    sent = structured_client.messages.created
    # Two different regions, one system prompt.
    assert len({item["system"] for item in sent}) == 1
    assert "eu-west-2" not in sent[0]["system"]
    # The region reached the model by riding on the brief instead.
    assert sent[0]["messages"][0]["content"].startswith("Build this in eu-west-2.")
    assert "A patient records system" in sent[0]["messages"][0]["content"]


def test_the_caller_s_own_conversation_is_not_rewritten(structured_client):
    """server.py names a conversation and stores it from this list."""
    messages = [{"role": "user", "content": "A shop"}]
    call_claude(
        structured_client, messages, "recommendation", constraints="Build this in eu-west-1."
    )

    assert messages == [{"role": "user", "content": "A shop"}]


def test_the_constraints_land_on_the_brief_not_on_a_follow_up(structured_client):
    """Mid-thread, the line belongs where it was written, or the prefix moves."""
    thread = [
        {"role": "user", "content": "A shop"},
        {"role": "assistant", "content": "{}"},
        {"role": "user", "content": "What does it cost?"},
    ]
    call_claude(structured_client, thread, "recommendation", constraints="Build this in eu-west-2.")

    sent = structured_client.messages.created[-1]["messages"]
    assert sent[0]["content"].startswith("Build this in eu-west-2.")
    assert sent[-1]["content"] == "What does it cost?"


# --------------------------------------------------------------------------- #
# Splitting the diagram out of a reply
# --------------------------------------------------------------------------- #


def test_the_fence_and_its_heading_come_out_together():
    diagram, prose = extract_mermaid(
        "Before\n\n### Architecture Diagram\n```mermaid\nflowchart LR\n  a-->b\n```\n\nAfter"
    )
    assert diagram == "flowchart LR\n  a-->b"
    assert prose == "Before\n\nAfter"


def test_a_reply_with_no_diagram_is_returned_whole():
    assert extract_mermaid("plain text") == (None, "plain text")


# --------------------------------------------------------------------------- #
# The request
# --------------------------------------------------------------------------- #


def test_the_request_sets_thinking_and_caching_deliberately(fake_client):
    call_claude(fake_client, [{"role": "user", "content": "hi"}])
    sent = fake_client.sent

    assert sent["model"] == advisor.MODEL
    assert sent["max_tokens"] == advisor.MAX_TOKENS
    assert sent["thinking"]["type"] == "adaptive"
    # R3: without a display setting the reasoning comes back empty, and the
    # first half-minute of a recommendation has nothing to show for itself.
    assert sent["thinking"]["display"] == "summarized"
    # R6: one breakpoint on the last block, so the growing history is cached.
    assert sent["cache_control"] == {"type": "ephemeral"}
    assert sent["timeout"] == advisor.TIMEOUT_SECONDS
    assert sent["messages"] == [{"role": "user", "content": "hi"}]


def test_max_tokens_leaves_room_for_thinking_and_a_reply():
    """R1: 3,000 was not enough for a full recommendation plus adaptive thinking."""
    assert advisor.MAX_TOKENS >= 8000


@pytest.mark.parametrize(
    ("kind", "effort"),
    [("recommendation", "high"), ("comparison", "high"), ("follow_up", "low")],
)
def test_effort_is_set_per_turn_not_once_for_everything(structured_client, kind, effort):
    """R5: the first recommendation and "break the cost down" are not the same job."""
    call_claude(structured_client, [{"role": "user", "content": "hi"}], kind)
    assert structured_client.sent["output_config"] == {"effort": effort}


def test_a_follow_up_asks_for_prose_and_a_recommendation_asks_for_json(structured_client):
    call_claude(structured_client, [{"role": "user", "content": "hi"}], "follow_up")
    # Passing output_format=None would have the SDK validate the reply against
    # NoneType, so a prose turn has to leave the argument out entirely.
    assert "output_format" not in structured_client.sent

    call_claude(structured_client, [{"role": "user", "content": "hi"}], "recommendation")
    assert structured_client.sent["output_format"] is Recommendation


def test_the_reply_text_and_its_cost_come_back_together(fake_client):
    reply = call_claude(fake_client, [{"role": "user", "content": "hi"}])
    assert reply.text == "hello"
    assert reply.recommendation is None
    assert reply.usage["calls"] == 1
    assert reply.usage["outputTokens"] == 500


def test_a_recommendation_comes_back_validated(structured_client):
    """R4: the shape is guaranteed by the schema, not guessed at afterwards."""
    reply = call_claude(structured_client, [{"role": "user", "content": "hi"}], "recommendation")

    assert reply.text == FULL_JSON
    assert reply.recommendation is not None
    assert len(reply.recommendation.services) == SERVICE_COUNT
    assert reply.recommendation.cost.tier.value == "Medium"


def test_a_recommendation_that_does_not_validate_is_an_error():
    client = FakeClient(fake_response(text='{"headline": "not a recommendation"}'))
    with pytest.raises(AdvisorError) as caught:
        call_claude(client, [{"role": "user", "content": "hi"}], "recommendation")
    assert caught.value.kind == "api"


def test_advise_wraps_a_single_workload(structured_client):
    reply = advise(structured_client, "An online shop")
    assert reply.recommendation is not None
    assert structured_client.sent["messages"] == [{"role": "user", "content": "An online shop"}]


# --------------------------------------------------------------------------- #
# Streaming (R3)
# --------------------------------------------------------------------------- #


def test_progress_is_reported_while_the_reply_is_written(fake_client):
    seen: list[Progress] = []
    call_claude(fake_client, [{"role": "user", "content": "hi"}], "follow_up", seen.append)

    assert len(seen) > 2
    # Reasoning arrives before any of the answer does, which is the whole point:
    # a first recommendation is mostly spent thinking.
    assert seen[0].thinking and not seen[0].text
    assert seen[-1].text == "hello"
    # Each snapshot is everything so far, not the delta on its own.
    texts = [step.text for step in seen]
    assert texts == sorted(texts, key=len)


def test_the_last_report_is_always_the_finished_reply(fake_client, monkeypatch):
    """Throttling must not swallow the final state of a fast reply."""
    monkeypatch.setattr(advisor, "PROGRESS_INTERVAL", 3600.0)
    seen: list[Progress] = []
    call_claude(fake_client, [{"role": "user", "content": "hi"}], "follow_up", seen.append)
    assert seen[-1].text == "hello"


def test_a_reply_needs_no_progress_callback(fake_client):
    assert call_claude(fake_client, [{"role": "user", "content": "hi"}]).text == "hello"


# --------------------------------------------------------------------------- #
# Stop reasons (R1)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("stop_reason", "kind", "phrase"),
    [
        ("max_tokens", "truncated", "ran out of room"),
        ("model_context_window_exceeded", "too_long", "too long for the model"),
        ("refusal", "refusal", "declined"),
    ],
)
def test_a_reply_that_did_not_finish_is_not_treated_as_a_success(stop_reason, kind, phrase):
    client = FakeClient(fake_response(stop_reason=stop_reason))

    with pytest.raises(AdvisorError) as caught:
        call_claude(client, [{"role": "user", "content": "hi"}])

    assert caught.value.kind == kind
    assert phrase in str(caught.value)
    # The call was billed even though it failed, so the cost is not lost.
    assert caught.value.usage["calls"] == 1
    assert caught.value.usage["inputTokens"] == 1000


def test_a_refusal_explains_itself_when_the_api_says_why():
    details = SimpleNamespace(category="cyber", explanation="policy decline")
    client = FakeClient(fake_response(stop_reason="refusal", stop_details=details))

    with pytest.raises(AdvisorError) as caught:
        call_claude(client, [{"role": "user", "content": "hi"}])
    assert "policy decline" in str(caught.value)


def test_a_response_with_no_text_is_an_api_error():
    client = FakeClient(fake_response(text=None))
    with pytest.raises(AdvisorError) as caught:
        call_claude(client, [{"role": "user", "content": "hi"}])
    assert caught.value.kind == "api"


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (APITimeoutError(request=SimpleNamespace()), "timeout"),
        (APIConnectionError(request=SimpleNamespace()), "connection"),
        (
            APIStatusError(
                "overloaded",
                response=SimpleNamespace(status_code=529, headers={}, request=None),
                body=None,
            ),
            "api",
        ),
        (rate_limited(), "rate_limit"),
    ],
)
def test_api_failures_become_advisor_errors(error, kind):
    client = FakeClient()
    client.messages.response = error

    with pytest.raises(AdvisorError) as caught:
        call_claude(client, [{"role": "user", "content": "hi"}])
    assert caught.value.kind == kind


# --------------------------------------------------------------------------- #
# Rate limiting (R7)
# --------------------------------------------------------------------------- #


def test_a_rate_limit_passes_on_the_window_the_api_asked_for():
    client = FakeClient()
    client.messages.response = rate_limited("30")

    with pytest.raises(AdvisorError) as caught:
        call_claude(client, [{"role": "user", "content": "hi"}])

    assert caught.value.retry_after == 30
    assert "30 seconds" in str(caught.value)


@pytest.mark.parametrize("header", [None, "not-a-number", ""])
def test_a_rate_limit_without_a_usable_window_still_reads_sensibly(header):
    client = FakeClient()
    client.messages.response = rate_limited(header)

    with pytest.raises(AdvisorError) as caught:
        call_claude(client, [{"role": "user", "content": "hi"}])

    assert caught.value.retry_after is None
    assert "rate limiting" in str(caught.value)


# --------------------------------------------------------------------------- #
# Pre-flight token counting (R2)
# --------------------------------------------------------------------------- #


def test_counting_tokens_uses_the_same_prompt_as_the_call():
    client = FakeClient(token_count=1234)
    assert count_input_tokens(client, [{"role": "user", "content": "hi"}]) == 1234
    assert client.messages.counted[-1]["system"] == advisor.system_prompt("recommendation")


def test_a_failed_count_does_not_block_the_call():
    client = FakeClient(token_count=APITimeoutError(request=SimpleNamespace()))
    assert count_input_tokens(client, [{"role": "user", "content": "hi"}]) == 0


# --------------------------------------------------------------------------- #
# A revision turn
# --------------------------------------------------------------------------- #


def test_a_revision_is_an_architecture_and_is_worked_at_like_one():
    """Not a question about the architecture: the architecture again.

    An opening brief is vague, so the first reply's sizes are estimates. What
    makes a deliverable worth handing over is what the conversation pinned down
    afterwards, and restating that is the same work as the first reply rather
    than a follow-up about it.
    """
    assert "revision" in advisor.STRUCTURED_KINDS
    assert advisor.EFFORT_BY_KIND["revision"] == "high"
    assert advisor.EFFORT_BY_KIND["revision"] == advisor.EFFORT_BY_KIND["recommendation"]


def test_a_revision_does_not_search():
    """Citations and a response format are mutually exclusive, and it is the format.

    The same trade the first recommendation makes. A follow-up is the turn that
    can go and check something; a revision is the turn that writes it down.
    """
    assert "revision" not in advisor.SEARCH_KINDS


def test_a_revision_is_asked_for_the_specifics_the_conversation_settled():
    """The instruction is the whole value of the feature, so it is asserted."""
    prompt = advisor.system_prompt("revision")

    assert "restate it in full against the schema" in prompt
    # Prefer what was agreed over the opening guess.
    assert "rather than the estimate you made from the opening brief" in prompt
    # And do not redecide the rest of the architecture on the way past.
    assert "change only what the conversation calls for" in prompt
    # A prose turn is told the opposite, and must not pick this up by accident.
    assert "restate it in full against the schema" not in advisor.system_prompt("follow_up")


def test_a_revision_comes_back_validated_against_the_schema(structured_client):
    """It goes down the same path the first recommendation does."""
    reply = advisor.call_claude(
        structured_client,
        [
            {"role": "user", "content": "A shop"},
            {"role": "assistant", "content": FULL_JSON},
            {"role": "user", "content": "Revise it."},
        ],
        kind="revision",
    )

    assert reply.recommendation is not None
    assert reply.recommendation.headline


# --------------------------------------------------------------------------- #
# Web search (F2)# --------------------------------------------------------------------------- #
# Web search (F2)
# --------------------------------------------------------------------------- #

QUOTAS = "https://docs.aws.amazon.com/general/latest/gr/aws_service_limits.html"
PRICES = "https://aws.amazon.com/rds/pricing/"


def searching_client(*responses) -> FakeClient:
    """A client whose follow-up reply cites pages, as a searched turn does."""
    return FakeClient(list(responses) if len(responses) > 1 else responses[0])


def test_a_follow_up_can_search_and_a_recommendation_cannot(structured_client):
    """Citations and a response format are mutually exclusive, so the turns split."""
    call_claude(structured_client, [{"role": "user", "content": "hi"}], "follow_up")
    assert structured_client.sent["tools"] == [advisor.WEB_SEARCH_TOOL]

    call_claude(structured_client, [{"role": "user", "content": "hi"}], "recommendation")
    assert "tools" not in structured_client.sent


def test_the_two_kinds_of_turn_can_never_overlap():
    """A turn asking for both a schema and a citation is a 400 from the API."""
    assert not (advisor.SEARCH_KINDS & advisor.STRUCTURED_KINDS)


def test_the_search_tool_is_capped_and_pointed_at_aws():
    tool = advisor.WEB_SEARCH_TOOL
    assert tool["type"] == "web_search_20260209"  # the variant Sonnet 5 supports
    # A turn that searched ten times has misunderstood the question.
    assert 1 <= tool["max_uses"] <= 8
    assert "docs.aws.amazon.com" in tool["allowed_domains"]
    assert all("aws" in domain for domain in tool["allowed_domains"])


def test_the_search_is_called_directly_so_that_it_cites_anything():
    """Verified against the real API: filtered through code execution, nothing cites."""
    assert advisor.WEB_SEARCH_TOOL["allowed_callers"] == ["direct"]


def test_a_cited_page_is_marked_in_the_answer_and_listed_under_it():
    client = searching_client(fake_response(text="Ten per region.", citations=[cited(QUOTAS)]))
    reply = call_claude(client, [{"role": "user", "content": "how many?"}])

    assert reply.text.startswith("Ten per region. [1]")
    assert advisor.SOURCES_HEADING in reply.text
    assert f"1. [AWS documentation]({QUOTAS})" in reply.text
    assert reply.sources == (advisor.Source("AWS documentation", QUOTAS),)


def test_a_marker_goes_after_the_cited_text_and_not_in_front_of_it():
    """Citations arrive at the start of their block, which the API confirmed live."""
    response = fake_response(text="Ten per region, ", citations=[cited(QUOTAS)])
    response.content.append(TextBlock(type="text", text="and it can be raised."))

    reply = call_claude(FakeClient(response), [{"role": "user", "content": "hi"}])
    # The marker sits before the space, so the next sentence still has one.
    assert reply.text.startswith("Ten per region, [1] and it can be raised.")


def test_two_pages_are_numbered_in_the_order_they_were_cited():
    client = searching_client(
        fake_response(text="Both.", citations=[cited(QUOTAS, "Quotas"), cited(PRICES, "Pricing")])
    )
    reply = call_claude(client, [{"role": "user", "content": "hi"}])

    assert "[1] [2]" in reply.text
    assert [source.title for source in reply.sources] == ["Quotas", "Pricing"]


def test_the_same_page_cited_twice_is_one_source_and_one_marker():
    client = searching_client(
        fake_response(text="Twice.", citations=[cited(QUOTAS), cited(QUOTAS)])
    )
    reply = call_claude(client, [{"role": "user", "content": "hi"}])

    assert len(reply.sources) == 1
    # One marker on the sentence, not "[1] [1]".
    assert reply.text.startswith("Twice. [1]\n")


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,x", "not a url at all"])
def test_a_source_the_front_end_would_not_render_is_dropped(url):
    """parse.py only renders three schemes, so a link it would drop is not written."""
    client = searching_client(fake_response(text="Careful.", citations=[cited(url)]))
    reply = call_claude(client, [{"role": "user", "content": "hi"}])

    assert reply.sources == ()
    assert reply.text == "Careful."


def test_a_title_carrying_brackets_cannot_break_the_link_it_is_in():
    client = searching_client(
        fake_response(text="Read it.", citations=[cited(QUOTAS, "Quotas [see also] notes")])
    )
    reply = call_claude(client, [{"role": "user", "content": "hi"}])

    assert reply.sources[0].title == "Quotas (see also) notes"
    assert f"[Quotas (see also) notes]({QUOTAS})" in reply.text


def test_citations_are_read_off_the_finished_reply_when_none_streamed():
    """Markers need a position in the text; a source list does not."""
    client = searching_client(fake_response(text="Checked.", citations=[cited(QUOTAS)]))
    client.messages.cite = False

    reply = call_claude(client, [{"role": "user", "content": "hi"}])
    assert reply.sources[0].url == QUOTAS
    assert "[1]" not in reply.text.split(advisor.SOURCES_HEADING)[0]
    assert QUOTAS in reply.text


def test_a_recommendation_is_never_given_a_sources_section(structured_client):
    """Appending Markdown to the JSON document would stop it validating."""
    reply = call_claude(structured_client, [{"role": "user", "content": "hi"}], "recommendation")
    assert reply.text == FULL_JSON
    assert reply.sources == ()


def test_a_paused_search_is_handed_back_to_finish():
    """A server-tool turn can stop with `pause_turn` and the work so far."""
    client = searching_client(
        fake_response(text="Looking it up", stop_reason="pause_turn", searches=1),
        fake_response(text=" -- ten per region.", citations=[cited(QUOTAS)]),
    )
    reply = call_claude(client, [{"role": "user", "content": "hi"}])

    assert reply.text.startswith("Looking it up -- ten per region. [1]")
    # Two calls, one turn: the second carries the first one's work as history.
    assert len(client.messages.created) == 2
    assert client.messages.created[1]["messages"][-1]["role"] == "assistant"
    assert reply.usage["calls"] == 2
    assert reply.usage["searches"] == 1


def test_a_turn_that_never_stops_pausing_gives_up():
    client = searching_client(fake_response(text="Still going", stop_reason="pause_turn"))

    with pytest.raises(AdvisorError) as caught:
        call_claude(client, [{"role": "user", "content": "hi"}])

    assert caught.value.kind == "paused"
    assert len(client.messages.created) == advisor.MAX_RESUMES + 1
    # Every one of those calls was billed, so the cost of giving up is not lost.
    assert caught.value.usage["calls"] == advisor.MAX_RESUMES + 1


# --------------------------------------------------------------------------- #
# A reply that ran out of room
# --------------------------------------------------------------------------- #


def truncating_stream(text: str):
    """A stream that fails the way the SDK fails on a reply cut off short.

    The real SDK parses the JSON when the content block closes, and a reply that
    hit the token cap closes its block like any other -- so the ValidationError
    comes out of iterating the stream, not out of get_final_message().
    """

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def __iter__(self):
            yield SimpleNamespace(type="content_block_stop", index=0)
            Recommendation.model_validate_json(text)  # raises, exactly as the SDK does

        def get_final_message(self):
            raise AssertionError("the stream should have raised first")

        @property
        def current_message_snapshot(self):
            return fake_response(text, stop_reason="max_tokens")

    return Stream()


def test_a_structured_reply_cut_off_short_is_reported_not_raised(monkeypatch):
    """It used to reach the browser as "something went wrong" and a traceback.

    The SDK validates the JSON as the content block closes, which is what a reply
    stopped at the token cap looks like, so it raises before stop_reason can be
    read and before _check_stop_reason gets its turn. Nothing caught it.
    """
    half = FULL_JSON[: len(FULL_JSON) // 2]
    client = FakeClient(fake_response(FULL_JSON))
    monkeypatch.setattr(client.messages, "stream", lambda **kwargs: truncating_stream(half))

    with pytest.raises(AdvisorError) as raised:
        call_claude(client, [{"role": "user", "content": "hi"}], "recommendation")

    assert raised.value.kind == "truncated"
    assert "ran out of room" in str(raised.value)
    assert f"{advisor.MAX_TOKENS:,}" in str(raised.value)
    # Billed like any other, so the ledger and the daily ceiling still hear about it.
    assert raised.value.usage["calls"] == 1


def test_a_reply_of_the_wrong_shape_is_not_called_a_truncation(monkeypatch):
    """Only an unreadable document is a truncation; a wrong one is its own bug."""
    wrong = json.dumps({"headline": "x", "overview": "y"})  # readable, but not this model
    client = FakeClient(fake_response(FULL_JSON))
    monkeypatch.setattr(client.messages, "stream", lambda **kwargs: truncating_stream(wrong))

    with pytest.raises(ValidationError):
        call_claude(client, [{"role": "user", "content": "hi"}], "recommendation")


def test_there_is_room_for_a_real_reply():
    """The cap covers the thinking and the whole document together.

    A measured recommendation runs to about 11,000 tokens all in, and 16,000 was
    close enough to that for a wordier one to be cut off mid-JSON.
    """
    assert advisor.MAX_TOKENS >= 32_000


# --------------------------------------------------------------------------- #
# Prices and usage (R2)
# --------------------------------------------------------------------------- #


def test_the_model_in_use_is_priced_at_its_published_rate():
    """C1. Sonnet 5 is $2/$10, and stays $2/$10 whatever the date.

    It was carried as $3/$15 -- Sonnet 4.6's rate, inherited when the model was
    bumped -- with an INTRO_PRICES entry overriding it to the real figure until
    31 August 2026. The test that used to sit here asserted the rise, so the
    whole app was one day away from over-reporting every cost by 50% with a
    green suite behind it. Pinned on both sides of that date for that reason.
    """
    for on in (date(2026, 8, 17), date(2026, 9, 1), date(2027, 1, 1)):
        assert rates("claude-sonnet-5", on) == {"input": 2.00, "output": 10.00}


def test_no_current_model_is_on_an_introductory_rate():
    """A standard rate that is only reachable past a date is the C1 trap.

    Nothing is on one now. If an entry is added here, the standard rate beside it
    has to be the one that actually applies afterwards -- which is the thing that
    was wrong before, and is not something the mechanism can check for itself.
    """
    assert advisor.INTRO_PRICES == {}


def test_an_introductory_rate_still_expires_on_its_own(monkeypatch):
    """The mechanism, kept under test with a model that does not exist.

    INTRO_PRICES is empty and worth keeping: the next model to launch on a
    promotional rate needs it, and it should not have to be rediscovered.
    """
    monkeypatch.setitem(advisor.PRICES, "claude-test-5", {"input": 8.00, "output": 40.00})
    monkeypatch.setitem(
        advisor.INTRO_PRICES,
        "claude-test-5",
        (date(2026, 8, 31), {"input": 4.00, "output": 20.00}),
    )
    assert rates("claude-test-5", date(2026, 8, 31))["input"] == 4.00
    assert rates("claude-test-5", date(2026, 9, 1))["input"] == 8.00


def test_a_model_we_have_no_price_for():
    assert rates("some-future-model") is None


def test_usage_prices_every_kind_of_token():
    usage = usage_of(
        fake_response(input_tokens=1_000_000, output_tokens=0, cache_read=1_000_000),
        model="claude-opus-5",
    )
    # $5.00 for the fresh million, a tenth of that for the cached million.
    assert usage["costUsd"] == pytest.approx(5.50)
    assert usage["priced"] is True


def test_a_cache_write_costs_a_quarter_more_than_a_read_would_have():
    usage = usage_of(
        fake_response(input_tokens=0, output_tokens=0, cache_write=1_000_000),
        model="claude-opus-5",
    )
    assert usage["costUsd"] == pytest.approx(6.25)


def test_an_unpriced_model_says_so_rather_than_reporting_nothing():
    usage = usage_of(fake_response(), model="some-future-model")
    assert usage["priced"] is False
    assert usage["costUsd"] == 0.0
    assert usage["inputTokens"] == 1000


def test_merging_adds_up_calls_tokens_and_cost():
    one = usage_of(fake_response())
    total = merge_usage(one, one, None)

    assert total["calls"] == 2
    assert total["inputTokens"] == 2000
    assert total["costUsd"] == pytest.approx(one["costUsd"] * 2)
    assert total["priced"] is True


def test_merging_nothing_is_a_zero_total():
    assert merge_usage()["calls"] == 0
    assert merge_usage()["costUsd"] == 0.0


def test_one_unpriced_call_makes_the_total_unpriced():
    total = merge_usage(usage_of(fake_response()), {"calls": 1, "priced": False})
    assert total["priced"] is False


def test_rubbish_from_a_client_cannot_skew_a_total():
    total = merge_usage({"calls": 1, "inputTokens": -500, "costUsd": -9.99})
    assert total["inputTokens"] == 0
    assert total["costUsd"] == 0.0


def test_a_search_is_billed_per_request_on_top_of_its_tokens():
    """F2: a search costs money that no token count would show."""
    usage = usage_of(fake_response(input_tokens=0, output_tokens=0, searches=4))

    assert usage["searches"] == 4
    assert usage["costUsd"] == pytest.approx(4 * advisor.SEARCH_PRICE_USD)


def test_a_turn_that_did_not_search_counts_no_searches():
    assert usage_of(fake_response())["searches"] == 0


def test_merging_adds_up_searches_too():
    searched = usage_of(fake_response(searches=2))
    assert merge_usage(searched, searched)["searches"] == 4


def test_the_usage_fields_are_the_same_in_both_languages():
    """A5: addUsage() in app.js keeps its own copy of this list, and it must match."""
    javascript = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    listed = re.search(r"const COUNT_FIELDS = \[(.*?)\];", javascript, re.DOTALL)
    assert listed, "COUNT_FIELDS is not in app.js at all"

    assert tuple(re.findall(r"'([A-Za-z]+)'", listed.group(1))) == advisor.COUNT_FIELDS


def test_the_fake_key_is_what_the_tests_run_with():
    """A guard on the fixture itself: no test should reach the real API."""
    assert get_client().api_key == FAKE_KEY
