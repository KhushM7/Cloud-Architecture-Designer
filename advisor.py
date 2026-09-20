"""Talking to the Claude API.

Everything the CLI (main.py) and the web app (server.py) share about asking
Claude for a recommendation: the model and how hard it is asked to think, the
system prompt, streaming, what a call cost, and what to do when one fails.

Where conversations are kept is store.py's job, not this module's.
"""

import logging
import os
import re
import time
from collections.abc import Callable, Sequence
from datetime import date
from typing import Any, Literal, NamedTuple

from anthropic import (
    Anthropic,
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    RateLimitError,
)
from anthropic.types import Message, MessageParam
from dotenv import load_dotenv
from pydantic import ValidationError

from schema import Compliance, Recommendation

# Read .env at import, so a key in the file is in os.environ before anything asks
# for a client. get_client() reads the environment when it is called, not here.
load_dotenv()

log = logging.getLogger("advisor")

MODEL = "claude-sonnet-5"

# max_tokens is a hard cap on thinking *and* reply text together, so it has to
# cover both. Adaptive thinking takes what it needs, and what is left has to hold
# the whole reply: a dozen services with their usage lines, the assumptions, six
# Well-Architected notes and a diagram is several thousand tokens of JSON, not the
# 1,500 an earlier version of this comment claimed. A real reply has been measured
# at 11,000 tokens all in, and one that ran past 16,000 is what sent a half-written
# document into the parser. Every call streams, so a generous cap costs nothing but
# the tokens actually produced -- where a small one costs a failed request.
MAX_TOKENS = 32_000

# Every reply streams, so the browser has something to show while Claude thinks
# and neither the SDK nor a proxy can time the request out mid-answer. This is
# the per-read timeout once streaming has started, not the total.
TIMEOUT_SECONDS = 120.0

# The SDK retries 429s and 5xx twice by default. Four is a better fit for a tool
# a person is sitting in front of: the extra attempts cost a few seconds of wait
# against a request that would otherwise fail outright.
MAX_RETRIES = 4

# How often a streaming call reports progress upwards. Fast enough to read as
# live, slow enough that the server is not re-rendering on every token.
PROGRESS_INTERVAL = 0.15

# What a turn is for. The first recommendation and a comparison are the whole
# point of the tool and get the deeper setting; a follow-up ("break the cost
# down") is a question about work already done and does not need it. Effort is
# the single biggest lever on both latency and cost per turn.
#
# A revision is the architecture restated after a conversation has changed it,
# and is the same work as the first recommendation rather than a question about
# it: an opening brief is vague, and what makes a deliverable worth handing over
# is the sizes and constraints the conversation pinned down afterwards. It is
# asked for explicitly -- nothing here infers that a follow-up meant to revise.
Kind = Literal["recommendation", "comparison", "follow_up", "revision"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]

EFFORT_BY_KIND: dict[str, Effort] = {
    "recommendation": "high",
    "comparison": "high",
    "follow_up": "low",
    "revision": "high",
}

# A recommendation is returned as JSON validated against schema.Recommendation;
# a follow-up is prose. Everything else about the calls is identical.
#
# A revision is in here because it is an architecture, and that is what makes the
# whole thing work: it arrives as the same Recommendation the first reply did, so
# parse.py draws it, pricing.py prices it, and export.py and terraform.py write it
# out with no idea that it is the second one. Nothing downstream needed changing.
STRUCTURED_KINDS = frozenset({"recommendation", "comparison", "revision"})

# F2. Claude's knowledge has a cutoff and AWS ships constantly, so a turn that
# can go and check is given the server-side web search tool. It runs on
# Anthropic's infrastructure: no scraping to maintain, no second API key, and
# the pages it read come back as citations rather than as a claim to take on
# trust.
#
# Only prose turns get it. A cited answer and a schema-constrained one are
# mutually exclusive -- citations are rejected outright alongside a response
# format -- and of the two, a recommendation whose shape is guaranteed is worth
# more than a cited one. So the first recommendation is still the model's own
# knowledge, and a follow-up ("is that instance family still current?", "what
# does that cost now?") is the turn that can look it up.
SEARCH_KINDS = frozenset({"follow_up"})

# Searches are billed per request, and a turn that runs away with them is worse
# value than one that answers from what it already knows.
MAX_SEARCHES = 5

# Whose pages count as an answer. AWS's own documentation is what makes a
# citation defensible in front of a client, and it is where service
# availability, quotas and list prices actually live. Widening this list is a
# one-line change; doing it means accepting whatever else the web says about AWS.
SEARCH_DOMAINS = [
    "aws.amazon.com",
    "docs.aws.amazon.com",
    "repost.aws",
    "calculator.aws",
]

# `allowed_callers` is the setting this feature turns on, and it is not the
# default. On web_search_20260209 the tool is called from inside code execution
# by default -- dynamic filtering, which writes code to cut the results down
# before they reach the context window and so costs fewer input tokens. Verified
# against the real API: a turn filtered that way answers from the pages but
# returns no citations at all, because what reached the model was code output
# rather than search results. Citations are the point here, so this asks for the
# direct call and pays the tokens.
WEB_SEARCH_TOOL: dict[str, Any] = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": MAX_SEARCHES,
    "allowed_domains": SEARCH_DOMAINS,
    "allowed_callers": ["direct"],
}

# A turn that used a server tool can come back paused rather than finished:
# searching takes real time, and the API hands back what it has instead of
# holding the connection open. Sending that straight back continues the same
# turn. This is how many times that is worth doing before treating the answer
# as stuck, which bounds both the wait and the bill.
#
# max_uses is counted per request rather than per turn, so a turn that pauses
# every time can search MAX_SEARCHES * (MAX_RESUMES + 1) times before it gives
# up. At the per-search rate that is a few pence, and it is the ceiling worth
# knowing about if either number is raised.
MAX_RESUMES = 3

# Sonnet 5 runs adaptive thinking whether or not you ask for it, so it is set
# here explicitly rather than left to an API default that could move under us.
#
# `display` asks for the reasoning to come back rather than be dropped. It costs
# nothing either way -- thinking is done and billed under both settings -- and
# it is what feeds the line under the progress dots. In practice Sonnet 5 starts
# writing the answer within a couple of seconds on these prompts and returns no
# separate reasoning at all, so treat that line as a bonus when it appears
# rather than as the thing that fills the wait. Streaming the answer is.
THINKING: dict[str, str] = {"type": "adaptive", "display": "summarized"}

# One breakpoint, on the last cacheable block of the request, so what gets
# cached is the conversation so far -- which is the part that grows. The system
# prompt alone is about 400 tokens, well under Sonnet 5's 1,024-token minimum
# cacheable prefix, so caching that on its own would silently do nothing.
#
# Two things follow from the prefix having to match byte for byte: the system
# prompt below is one fixed string with nothing interpolated into it, and the
# first turn of a thread never reads from cache (there is no prefix yet, and it
# is a structured call where every later turn is prose). Reads start at the
# second follow-up. `usage.cacheReadTokens` in the sidebar is the check.
#
# The tool list renders ahead of the system prompt, so it is part of the prefix
# too. WEB_SEARCH_TOOL is a fixed dict and every prose turn sends it, which is
# what keeps those turns sharing one prefix.
CACHE_CONTROL: dict[str, str] = {"type": "ephemeral"}

# Who the model is and how it writes, on every turn. What the turn *is* comes
# from INSTRUCTION_BY_KIND below and is appended to this.
SYSTEM_PROMPT = """You are an AWS Architecture Advisor, an AWS solutions architect \
advising UK organisations, including public sector bodies.

A user describes a business problem or workload in plain English and you recommend an AWS \
architecture for it. Be specific and opinionated: name the services you would actually use, \
say why, and say what you would watch out for. Every cost you give is in US dollars, the \
currency AWS publishes its list prices in.

Assume the person reading you is not an AWS engineer. They know their business and their \
problem; they do not know what a shard, a read replica or a placement group is until you \
tell them, and they will not know what to ask you next unless you make it obvious.

A brief may open with a line naming the region the architecture must run in and a \
compliance regime it has to stand up to. Those are the user's decisions, not \
suggestions: build in that region, and let the regime show up in the services you \
pick and in the Well-Architected notes rather than being mentioned once and dropped."""

# What this turn is, appended to the prompt above.
#
# Per kind rather than one prompt describing all four, because the failure it
# prevents is the worst this file has: a prose turn that answers with the schema.
# Asked for specific instance sizes, a follow-up would decide it had been "asked
# for an architecture", and return several thousand characters of raw JSON into a
# chat bubble. The schema is only ever right when the reply is actually
# constrained to it, and only the server knows which turn this is.
#
# It costs nothing to split. `tools` is rendered before `system` in the cache
# prefix, and `follow_up` is the only kind that sends a tool, so a follow-up
# never shared a prefix with a structured turn in the first place. Only
# `recommendation` and `revision` newly diverge, and both are rare turns where a
# few hundred uncached tokens are noise against the reply itself.
INSTRUCTION_BY_KIND: dict[str, str] = {
    "recommendation": """This turn is the first architecture for a workload. Your reply is \
constrained to a JSON schema and the field descriptions tell you what each part is for. \
Fill every field.

The brief you have been given is probably vague about scale, and you will have to size \
things anyway. Do it, put what you took as read in the assumptions field rather than \
leaving it in the prose, and pick sizes you would defend rather than the smallest ones \
that fit. Put a monthly dollar figure on every line of usage, including \
the lines you expect can be looked up -- what cannot be looked up is shown as your figure, \
and what can is checked against it.""",
    "comparison": """This turn is one of several architectures being weighed against each \
other for the same workload. Your reply is constrained to a JSON schema and the field \
descriptions tell you what each part is for. Fill every field.

Argue for this approach on its own terms. You are not being shown the others, so do \
not hedge against them or describe them.""",
    "follow_up": """This turn is a question about a recommendation you have already given. \
Answer it in Markdown prose, conversationally and concisely, referring back to that \
recommendation rather than restating it.

Never answer this turn with JSON, a schema, a services table or a full architecture, \
however the question is phrased. Asked to recommend specific sizes, instance types, \
quantities or capacity settings, give exactly those specifics -- a short list, or a \
sentence each, with a line of reasoning -- and leave the architecture where it is. Asked \
for a list of the services needed, name them and what each is for, in prose. The user has \
a button for rebuilding the architecture and this turn is not it, so restating it here \
just buries the answer they asked for.

Use web search for anything that dates: whether a service, instance family or feature is \
still current, what a service quota is, what something costs today. Search when the answer \
turns on a current fact rather than on every turn, say in the answer what you checked, and \
do not present a searched figure as more precise than the page you read it from.""",
    "revision": """This turn rebuilds the architecture. The user pressed a button to ask \
for it, so restate it in full against the schema, as it now stands after everything the \
conversation has settled.

Two rules. Where the conversation has pinned down a size, an instance type, a count, an \
engine, a region or a constraint, use what was agreed rather than the estimate you made \
from the opening brief; that specificity is the whole reason a revision is worth asking \
for. And change only what the conversation calls for -- carry everything it did not touch \
through unchanged, sizes and service names included, so that what moved is what was \
discussed and nothing else.

Price the sizes you have landed on, not the ones you guessed from the opening brief.

Where the conversation has corrected something you took as read, that correction is the \
assumption now: restate it as the reader gave it, and let the sizing follow from it \
rather than from the figure you first guessed.""",
}


# What each compliance regime means, in the words the model is given (F5). The
# slugs are schema.Compliance; the sentences are here, because this is where the
# rest of the prompt lives and they are prompt rather than data.
#
# Each one says what to do rather than naming the standard and hoping: "PCI DSS"
# on its own invites a paragraph about PCI DSS, where the point is that the
# architecture comes back different.
COMPLIANCE_RULES: dict[str, str] = {
    Compliance.UK_DATA_RESIDENCY: "All data must stay in the United Kingdom. Do not "
    "recommend a region outside it, and call out any service that would move data or "
    "metadata out of it.",
    Compliance.PCI_DSS: "Cardholder data is in scope for PCI DSS. Keep the environment "
    "that touches it segmented from everything else, and say what is in scope and what "
    "is deliberately kept out of it.",
    Compliance.HIPAA: "Protected health information is in scope for HIPAA. Recommend only "
    "HIPAA-eligible services, and treat encryption in transit and at rest and an audit "
    "trail as requirements rather than options.",
    Compliance.NHS_DSPT: "This is for an NHS organisation and has to satisfy the Data "
    "Security and Protection Toolkit. Keep patient data in the UK, and be specific about "
    "access control, audit logging and how long data is kept.",
}


def constraints_line(region: str = "", compliance: str = "") -> str:
    """The line that pins a brief to a region and a regime, or "" if it is free (F5).

    This is prepended to the brief rather than added to the system prompt,
    because the system prompt is one fixed string and interpolating a region into
    it would void the cache prefix on every turn of every conversation. Inside a
    conversation the same line is reproduced byte for byte on each turn, so it
    caches like the rest of the thread.
    """
    parts: list[str] = []
    if region:
        parts.append(f"Build this in {region}.")
    rule = COMPLIANCE_RULES.get(compliance) if compliance else None
    if rule:
        parts.append(rule)
    return " ".join(parts)


def with_constraints(messages: list[MessageParam], constraints: str) -> list[MessageParam]:
    """Pin the brief to its constraints, without disturbing anything else.

    Folded into the first user turn rather than added as a turn of its own, so
    the thread still alternates and there is no question of what the model does
    with two user messages in a row.

    The caller's list is left alone. That matters: server.py names a conversation
    from the first user message and stores what the client sent, and neither
    should show a reader the plumbing.
    """
    if not constraints:
        return messages

    patched = list(messages)
    for index, message in enumerate(patched):
        if message["role"] == "user":
            patched[index] = {
                "role": "user",
                "content": f"{constraints}\n\n{message['content']}",
            }
            break
    return patched


def system_prompt(kind: str) -> str:
    """The prompt for one turn: who the model is, then what this turn is."""
    instruction = INSTRUCTION_BY_KIND.get(kind)
    if not instruction:
        return SYSTEM_PROMPT
    return f"{SYSTEM_PROMPT}\n\n{instruction}"


MERMAID_PATTERN = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL)
DIAGRAM_HEADING_PATTERN = re.compile(r"#+\s*Architecture Diagram\s*\n*")


class AdvisorError(Exception):
    """A user-facing failure when talking to the Claude API.

    `kind` lets a caller style the failure differently without matching on the
    message text. The CLI ignores it and just prints str(error).

    `usage` carries the tokens a failed call still consumed -- a reply truncated
    at the cap is billed like any other -- so the running cost stays honest.

    `retry_after` is the number of seconds the API asked us to wait, on a 429.
    """

    def __init__(
        self,
        message: str,
        kind: str = "error",
        usage: dict[str, Any] | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.usage = usage
        self.retry_after = retry_after


# --------------------------------------------------------------------------- #
# Token and cost accounting
# --------------------------------------------------------------------------- #

# US dollars per million tokens, from the Claude API pricing page. Anthropic
# bills in USD, so no exchange rate is invented here.
#
# Sonnet 5 is $2/$10. It was carried here as $3/$15 with an INTRO_PRICES entry
# overriding it to $2/$10 until 31 August 2026 -- but $3/$15 is Sonnet 4.6's
# rate, inherited when the model was bumped, and $2/$10 is not introductory. Left
# alone, every figure in the app would have risen 50% on 1 September for no
# reason, and the ledger would have carried both scales with nothing to tell them
# apart.
PRICES: dict[str, dict[str, float]] = {
    "claude-sonnet-5": {"input": 2.00, "output": 10.00},
    "claude-opus-5": {"input": 5.00, "output": 25.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
}

# A model launched on introductory pricing goes here: {model: (last day, rates)}.
# Past the end date the standard rate above applies, so the figures shown do not
# quietly go stale. Empty because no current model is on one -- kept because the
# next one will be, and because rates() is what a test pins a dated rate against.
INTRO_PRICES: dict[str, tuple[date, dict[str, float]]] = {}

# A cache read costs a tenth of the input rate; writing a five-minute cache entry
# costs a quarter more than reading those tokens fresh.
CACHE_READ_RATE = 0.1
CACHE_WRITE_RATE = 1.25

# A web search is billed per request rather than per token, at $10 per thousand,
# and the same for every model. The tokens the results bring back are billed as
# input on top, and are already counted as such.
SEARCH_PRICE_USD = 10.00 / 1_000

TOKEN_FIELDS = ("calls", "inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens")

# Everything counted per call, which is the token fields plus searches. Kept
# apart from TOKEN_FIELDS because a search is not a token: anything summing
# tokens wants the list above, and anything storing or merging a usage record
# wants this one. Mirrored by COUNT_FIELDS in static/app.js.
COUNT_FIELDS = (*TOKEN_FIELDS, "searches")


def rates(model: str = MODEL, on: date | None = None) -> dict[str, float] | None:
    """Return the per-million-token rates for a model, or None if unlisted."""
    intro = INTRO_PRICES.get(model)
    if intro and (on or date.today()) <= intro[0]:
        return intro[1]
    return PRICES.get(model)


def usage_of(response: Message, model: str = MODEL) -> dict[str, Any]:
    """Read one call's token counts off a response and price them.

    `priced` is False when the model is not in the table above, so the UI can say
    it does not know rather than report a cost of zero.
    """
    used = response.usage
    # Only a turn carrying the search tool has server tool use on it at all.
    server_tools = getattr(used, "server_tool_use", None)
    counted: dict[str, Any] = {
        "calls": 1,
        "inputTokens": used.input_tokens or 0,
        "outputTokens": used.output_tokens or 0,
        "cacheReadTokens": used.cache_read_input_tokens or 0,
        "cacheWriteTokens": used.cache_creation_input_tokens or 0,
        "searches": int(getattr(server_tools, "web_search_requests", 0) or 0),
    }

    rate = rates(model)
    counted["priced"] = rate is not None
    counted["costUsd"] = (
        0.0
        if rate is None
        else (
            counted["inputTokens"] * rate["input"]
            + counted["cacheReadTokens"] * rate["input"] * CACHE_READ_RATE
            + counted["cacheWriteTokens"] * rate["input"] * CACHE_WRITE_RATE
            + counted["outputTokens"] * rate["output"]
        )
        / 1_000_000
        + counted["searches"] * SEARCH_PRICE_USD
    )
    return counted


def merge_usage(*totals: dict[str, Any] | None) -> dict[str, Any]:
    """Add up any number of usage records. Mirrors addUsage() in static/app.js."""
    merged: dict[str, Any] = dict.fromkeys(COUNT_FIELDS, 0)
    merged["costUsd"] = 0.0
    merged["priced"] = True

    for total in totals:
        if not total:
            continue
        for field in COUNT_FIELDS:
            merged[field] += max(0, int(total.get(field) or 0))
        merged["costUsd"] += max(0.0, float(total.get("costUsd") or 0.0))
        merged["priced"] = merged["priced"] and bool(total.get("priced", True))
    return merged


class Progress(NamedTuple):
    """A snapshot of a reply while it is still being written.

    `thinking` is Claude's summarised reasoning, which on a first recommendation
    arrives well before any of the answer does.
    """

    text: str
    thinking: str


class Source(NamedTuple):
    """One page Claude searched and cited, as it is listed under the answer."""

    title: str
    url: str


class Reply(NamedTuple):
    """A reply, what the call that produced it cost, and its parsed form.

    `text` is what goes into the conversation history and onto disk: the JSON
    document for a recommendation, Markdown for a follow-up. `recommendation` is
    that same JSON already validated, and is None for a follow-up.

    `sources` is what a searched turn cited, in the order the numbered markers
    in `text` refer to them. It is a convenience for a caller that wants the
    pages as data; the answer already carries them as Markdown links.
    """

    text: str
    usage: dict[str, Any]
    recommendation: Recommendation | None = None
    sources: tuple[Source, ...] = ()


# One client per API key, reused for every call. Anthropic() opens its own
# connection pool, so building one per request would mean a fresh TLS handshake
# each time.
_clients: dict[str, Anthropic] = {}


def get_client(api_key: str | None = None) -> Anthropic:
    """Return a client for the API key in use, raising AdvisorError if there is none."""
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        # Re-read .env, so a key added after start-up is picked up by the web
        # app's "Check again" without a restart.
        load_dotenv()
        key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise AdvisorError(
            "ANTHROPIC_API_KEY is missing. Set it in your .env file or environment "
            "before running this tool.",
            kind="missing_key",
        )

    client = _clients.get(key)
    if client is None:
        client = _clients[key] = Anthropic(api_key=key, max_retries=MAX_RETRIES)
    return client


def count_input_tokens(
    client: Anthropic, messages: list[MessageParam], kind: str = "recommendation"
) -> int:
    """Count what a conversation would cost to send, without sending it.

    Used as a pre-flight guard on long threads. Returns 0 if the count itself
    fails, so a hiccup here never blocks the real call.
    """
    try:
        counted = client.messages.count_tokens(
            model=MODEL,
            system=system_prompt(kind),
            messages=messages,
        )
    except (APIConnectionError, APIStatusError, APITimeoutError) as e:
        log.warning("token count failed, sending anyway: %s", e)
        return 0
    return counted.input_tokens


# --------------------------------------------------------------------------- #
# Talking to Claude
# --------------------------------------------------------------------------- #


def _request_id(error: Exception) -> str:
    """The API's id for a failed call, for reporting it to Anthropic."""
    return getattr(error, "request_id", None) or "unknown"


def _retry_after(error: RateLimitError) -> int | None:
    """How long the API asked us to wait, if it said."""
    header = error.response.headers.get("retry-after") if error.response is not None else None
    try:
        return max(1, int(float(header))) if header else None
    except (TypeError, ValueError):
        return None


def _translate(error: Exception) -> AdvisorError:
    """Turn an SDK exception into something worth showing a user.

    Everything here is logged with the API's request id first: it is the only
    handle Anthropic support has on a specific failed call, and it is gone once
    the exception is swallowed.
    """
    if isinstance(error, APITimeoutError):
        log.error("request timed out after %.0fs [%s]", TIMEOUT_SECONDS, _request_id(error))
        return AdvisorError(
            f"The request to Claude timed out after {TIMEOUT_SECONDS:.0f} seconds. "
            "Please try again.",
            kind="timeout",
        )

    if isinstance(error, APIConnectionError):
        log.error("could not reach the API: %s", error)
        return AdvisorError(
            "Could not connect to the Claude API. Check your network connection.",
            kind="connection",
        )

    if isinstance(error, RateLimitError):
        # The SDK has already retried this MAX_RETRIES times before it reaches
        # us, so the limit is not clearing on its own and the user needs to know
        # how long to leave it.
        wait = _retry_after(error)
        log.warning("rate limited, retry-after %ss [%s]", wait or "unset", _request_id(error))
        return AdvisorError(
            "Claude is rate limiting this API key"
            + (f". Try again in {wait} seconds." if wait else ", after several retries.")
            + " If this keeps happening, check your usage limits in the Anthropic console.",
            kind="rate_limit",
            retry_after=wait,
        )

    if isinstance(error, APIStatusError):
        log.error("API returned %s: %s [%s]", error.status_code, error.message, _request_id(error))
        return AdvisorError(
            f"Claude API returned an error (status {error.status_code}): {error.message}",
            kind="api",
        )

    raise error


# Said in one place because it is reached two ways: a prose reply that stops at
# the cap is caught by its stop_reason below, and a structured one never gets that
# far -- the SDK parses the JSON as the content block closes and raises first.
RAN_OUT_OF_ROOM = (
    f"Claude ran out of room before finishing: thinking and reply together hit the "
    f"{MAX_TOKENS:,}-token limit. Ask a narrower question, or raise MAX_TOKENS in "
    "advisor.py."
)


def _truncated_json(error: ValidationError) -> bool:
    """Whether a reply stopped mid-JSON, as against coming back the wrong shape.

    `json_invalid` is pydantic saying it could not read the document at all, which
    for a schema-constrained reply means it was cut off. A reply that parsed but
    did not fit the model raises something else, and that is a different problem
    with a different answer, so it is left to propagate.
    """
    return any(detail.get("type") == "json_invalid" for detail in error.errors())


def _check_stop_reason(response: Message, usage: dict[str, Any]) -> None:
    """Refuse to hand back a reply that is not actually finished.

    A truncated or declined reply comes back as a perfectly normal success, so
    without this the UI would render half a recommendation and say nothing.
    These calls are still billed, hence the usage on the error.
    """
    if response.stop_reason == "max_tokens":
        raise AdvisorError(RAN_OUT_OF_ROOM, kind="truncated", usage=usage)

    if response.stop_reason == "model_context_window_exceeded":
        raise AdvisorError(
            "This conversation is too long for the model's context window. Save it and "
            "start a new one.",
            kind="too_long",
            usage=usage,
        )

    if response.stop_reason == "pause_turn":
        raise AdvisorError(
            f"Claude was still searching after {MAX_RESUMES} attempts to let it finish. "
            "Ask again, or ask something narrower.",
            kind="paused",
            usage=usage,
        )

    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        reason = getattr(details, "explanation", None) or getattr(details, "category", None)
        raise AdvisorError(
            "Claude declined to answer this one"
            + (f" ({reason})." if reason else ".")
            + " Rephrase the workload and try again.",
            kind="refusal",
            usage=usage,
        )


# A cited page is listed under the answer as an ordinary Markdown link, which
# means parse.py renders it and md_to_html's scheme check applies to it like any
# other. Markdown has no escape for a bracket inside link text, and the link
# pattern stops at the first one, so a title carrying them is cleaned instead.
SOURCE_TITLE_CHARS = str.maketrans({"[": "(", "]": ")"})
MAX_SOURCE_TITLE = 90
SOURCES_HEADING = "#### Sources"


def _source(citation: Any) -> Source | None:
    """Read one citation off the stream as a listable source, or None.

    None covers anything that would not survive being written as a Markdown
    link: a scheme the front end will not render, or whitespace in the URL.
    """
    url = str(getattr(citation, "url", "") or "").strip()
    if not url.lower().startswith(("http://", "https://")) or url.split() != [url]:
        return None
    # Parentheses would close the link early, so they go as the escapes a URL
    # already has for them.
    url = url.replace("(", "%28").replace(")", "%29")

    named = str(getattr(citation, "title", "") or "").translate(SOURCE_TITLE_CHARS)
    title = " ".join(named.split())
    if len(title) > MAX_SOURCE_TITLE:
        title = title[: MAX_SOURCE_TITLE - 1].rstrip() + "…"
    return Source(title or url, url)


def _log_search_failure(block: Any, message_id: str) -> None:
    """Note a search that failed, which the API reports as a successful reply.

    A failed search comes back as a result block whose content is a single error
    object rather than a list of pages, and Claude usually answers from what it
    already knows instead. That is a reasonable outcome and not worth failing the
    turn over, but it is worth knowing about: it is the difference between an
    answer that was checked and one that only looks like it was.
    """
    if getattr(block, "type", "") != "web_search_tool_result":
        return
    content = getattr(block, "content", None)
    error_code = getattr(content, "error_code", None)
    if error_code:
        log.warning("web search failed (%s), answering without it [%s]", error_code, message_id)


def _sources_markdown(sources: Sequence[Source]) -> str:
    """The numbered list the markers in a searched answer point at."""
    listed = "\n".join(
        f"{number}. [{source.title}]({source.url})"
        for number, source in enumerate(sources, start=1)
    )
    return f"\n\n{SOURCES_HEADING}\n\n{listed}\n"


def call_claude(
    client: Anthropic,
    messages: list[MessageParam],
    kind: Kind = "follow_up",
    on_progress: Callable[[Progress], None] | None = None,
    constraints: str = "",
) -> Reply:
    """Send the conversation to Claude and return the reply with its token usage.

    `kind` picks how hard the model works, whether the reply comes back as a
    validated Recommendation or as prose, and whether the turn can search the web
    (F2). `on_progress` is called every PROGRESS_INTERVAL seconds with everything
    written so far, which is what the web app renders while it waits.
    `constraints` is the region and compliance line from constraints_line(), or ""
    (F5); it is folded into the brief here rather than by the caller, so the
    conversation the caller keeps and stores stays the one the user wrote.

    A searched turn can come back paused rather than finished, and is sent
    straight back to continue, up to MAX_RESUMES times. What that costs is added
    up across every round, so one Reply covers the whole turn either way.

    Raises AdvisorError on any API failure, so callers can present it however
    suits.
    """
    structured = kind in STRUCTURED_KINDS
    started = time.monotonic()
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    sources: list[Source] = []
    numbered: dict[str, int] = {}
    pending: list[int] = []
    reported = 0.0

    def report(force: bool = False) -> None:
        nonlocal reported
        now = time.monotonic()
        if on_progress and (force or now - reported >= PROGRESS_INTERVAL):
            reported = now
            on_progress(Progress("".join(text_parts), "".join(thinking_parts)))

    def register(citation: Any) -> int | None:
        """Number a cited page, reusing the number if it has been cited before."""
        source = _source(citation)
        if source is None:
            return None
        number = numbered.get(source.url)
        if number is None:
            number = numbered[source.url] = len(sources) + 1
            sources.append(source)
        return number

    def cite(citation: Any) -> None:
        """Note a citation, to be marked when the text it belongs to is done."""
        number = register(citation)
        # A block citing the same page twice gets one marker, not two.
        if number is not None and number not in pending:
            pending.append(number)

    def mark() -> None:
        """Put the markers for the block just finished at the end of its text.

        Citations arrive at the *start* of the text block they belong to, so a
        marker written when one lands would sit in front of the sentence it
        refers to. They are held until the block ends and then written after its
        last word -- before any trailing space, so the next sentence still has
        one in front of it.
        """
        if not pending:
            return
        written = "".join(text_parts)
        ended = written.rstrip()
        markers = "".join(f" [{number}]" for number in pending)
        text_parts.clear()
        text_parts.append(ended + markers + written[len(ended) :])
        pending.clear()

    # output_format has to be left out entirely for a prose turn rather than
    # passed as None: the SDK treats None as "an actual format of NoneType" and
    # tries to validate the reply against it.
    schema_arg: dict[str, Any] = {"output_format": Recommendation} if structured else {}
    tools_arg: dict[str, Any] = {"tools": [WEB_SEARCH_TOOL]} if kind in SEARCH_KINDS else {}

    turn: list[MessageParam] = with_constraints(messages, constraints)
    spent: list[dict[str, Any]] = []

    for _ in range(MAX_RESUMES + 1):
        try:
            with client.messages.stream(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                thinking=THINKING,  # type: ignore[arg-type]
                output_config={"effort": EFFORT_BY_KIND[kind]},
                cache_control=CACHE_CONTROL,  # type: ignore[arg-type]
                system=system_prompt(kind),
                messages=turn,
                timeout=TIMEOUT_SECONDS,
                **tools_arg,
                **schema_arg,
            ) as stream:
                for event in stream:
                    if event.type == "content_block_stop":
                        mark()
                    elif event.type == "content_block_delta":
                        if event.delta.type == "text_delta":
                            text_parts.append(event.delta.text)
                        elif event.delta.type == "thinking_delta":
                            thinking_parts.append(event.delta.thinking)
                        elif event.delta.type == "citations_delta":
                            cite(event.delta.citation)
                        else:
                            continue
                    else:
                        continue
                    report()
                mark()  # in case the last block ended with the stream
                response = stream.get_final_message()
        except (APIConnectionError, APIStatusError, APITimeoutError) as e:
            raise _translate(e) from e
        except ValidationError as e:
            # A structured reply that ran out of room. The SDK parses the JSON as
            # the content block closes, and a reply cut off at the cap closes its
            # block like any other, so this arrives here rather than at the
            # stop_reason check below. Same failure, same sentence. Anything else
            # wrong with the shape of a reply is not this, and is left alone.
            if not _truncated_json(e):
                raise
            log.error("structured reply stopped mid-JSON: %s", e.errors()[0].get("msg"))
            # Billed like any other, so the ledger still hears about it: the
            # snapshot is what the stream had accumulated when it gave up.
            snapshot = getattr(stream, "current_message_snapshot", None)
            raise AdvisorError(
                RAN_OUT_OF_ROOM,
                kind="truncated",
                usage=usage_of(snapshot) if snapshot is not None else None,
            ) from e

        spent.append(usage_of(response))
        # Every citation is on the finished message as well as in the stream, so
        # a page that was cited without a delta of its own is still listed. A
        # page already numbered adds nothing here.
        for block in response.content:
            for citation in getattr(block, "citations", None) or []:
                register(citation)
            _log_search_failure(block, response.id)

        if response.stop_reason != "pause_turn":
            break
        # A paused turn is continued by handing it back, search results and all.
        turn = [*turn, {"role": "assistant", "content": response.content}]

    usage = merge_usage(*spent)
    report(force=True)
    searched = f", {usage['searches']} searches" if usage["searches"] else ""
    log.info(
        "%s reply in %.1fs: %s in / %s out, %s cached%s [%s]",
        kind,
        time.monotonic() - started,
        usage["inputTokens"],
        usage["outputTokens"],
        usage["cacheReadTokens"],
        searched,
        response.id,
    )

    _check_stop_reason(response, usage)

    text = "".join(text_parts)
    if not text.strip():
        raise AdvisorError(
            "Claude returned a response with no text content.", kind="api", usage=usage
        )

    if not structured:
        # The pages go under the answer, where the markers in it point.
        if sources:
            text = text.rstrip() + _sources_markdown(sources)
        return Reply(text, usage, None, tuple(sources))

    # output_format guarantees valid JSON against the schema, and the SDK has
    # already validated it. The fallback is for the one case it cannot cover: a
    # reply that stopped early for a reason not caught above.
    parsed = getattr(response, "parsed_output", None)
    if parsed is None:
        try:
            parsed = Recommendation.model_validate_json(text)
        except ValueError as e:
            log.error("structured reply did not validate [%s]: %s", response.id, e)
            raise AdvisorError(
                "Claude's reply did not match the recommendation format. Try again.",
                kind="api",
                usage=usage,
            ) from e
    return Reply(text, usage, parsed)


def advise(
    client: Anthropic,
    workload: str,
    kind: Kind = "recommendation",
    on_progress: Callable[[Progress], None] | None = None,
    constraints: str = "",
) -> Reply:
    """Convenience wrapper for a single one-shot recommendation."""
    return call_claude(
        client, [{"role": "user", "content": workload}], kind, on_progress, constraints
    )


def extract_mermaid(text: str) -> tuple[str | None, str]:
    """Split a Markdown reply into its Mermaid diagram and the remaining prose.

    Only follow-ups need this now: a recommendation carries its diagram in its
    own field. A follow-up that revises the architecture often includes a fresh
    diagram in a fenced block, and it should still be drawn.

    Returns (diagram_source or None, text with the diagram block removed).
    """
    match = MERMAID_PATTERN.search(text)
    if not match:
        return None, text

    remainder = DIAGRAM_HEADING_PATTERN.sub("", text[: match.start()] + text[match.end() :])
    return match.group(1).strip(), remainder.strip()
