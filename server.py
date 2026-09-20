"""Architecture Advisor - web app.

Serves the front end in static/ and exposes advisor.py over JSON.

Run with:  python server.py
"""

import json
import logging
import os
import queue
import secrets
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from anthropic.types import MessageParam
from flask import Flask, Response, jsonify, request, session
from flask_limiter import Limiter
from flask_limiter.errors import RateLimitExceeded
from flask_limiter.util import get_remote_address

import branding
import export
import pricing
import store
from advisor import (
    MODEL,
    STRUCTURED_KINDS,
    AdvisorError,
    Kind,
    Progress,
    Reply,
    advise,
    call_claude,
    constraints_line,
    count_input_tokens,
    extract_mermaid,
    get_client,
    merge_usage,
)
from parse import md_to_html, partial_json, prose_payload, recommendation_payload
from schema import Compliance, Recommendation, Region

log = logging.getLogger("advisor.server")

ROOT = Path(__file__).parent
STATIC_DIR = ROOT / "static"
README = ROOT / "README.md"

KNOWN_REGIONS = frozenset(region.value for region in Region)

# The brand assets and fonts never change within a release, so let the browser
# keep them rather than revalidating on every page load.
ASSET_MAX_AGE = 7 * 24 * 60 * 60

# Request limits. MAX_CONTENT_LENGTH is Flask's own ceiling on the request body;
# the rest bound what actually reaches the API and the disk, because a megabyte
# of valid JSON is still far more than a workload description needs.
MAX_BODY_BYTES = 1024 * 1024
MAX_MESSAGE_CHARS = 20_000
MAX_CONVERSATION_CHARS = 400_000
MAX_MESSAGES = 60

# Characters are a cheap proxy for size; tokens are the real currency, and dense
# text (code, JSON, non-English) tokenises far worse than prose. Past this many
# characters the count is worth a round trip before committing to the call.
PREFLIGHT_CHARS = 80_000
MAX_INPUT_TOKENS = 120_000

# Spend guards (S4). Every /api/advise costs money, and the two ways to spend it
# by accident are a loop in an open browser tab and a retry that never gives up.
#
# None of this is a security control, and it is not meant to be: without
# authentication (S3) a determined client can drop its cookie and come back, and
# per-IP limits mean little behind a shared address. What these do is bound the
# damage an accident can do. The ceiling is the one that bounds the money.
SESSION_LIMITS = "10 per minute; 100 per hour"
IP_LIMITS = "30 per minute; 300 per hour"

# How many architectures one comparison may weigh up (F9). Two is the least that
# is a comparison at all; four is where the columns stop being readable and the
# turn stops being one anybody waits for.
MIN_COMPARE = 2
MAX_COMPARE = 4

# Pricing an architecture is cheap once a region's prices are cached and slow the
# first time, because the EC2 price list is 200 MB. It costs no Anthropic money,
# so it gets its own allowance rather than eating into the one above.
ESTIMATE_LIMITS = "30 per minute; 300 per hour"

# Tokens, all kinds counted together, per UK day across the CLI and the web app.
# The number and the reasoning behind it now live beside the ledger they bound, so
# `python store.py --report` can name the same ceiling this enforces (F14).
DAILY_TOKEN_CEILING = store.daily_ceiling()

# Defence in depth for the Markdown the front end injects with innerHTML: even if
# a tag or a scheme slipped past parse.py, the browser has nowhere to load a
# script from and no way to phone one home. 'unsafe-inline' covers the one inline
# style attribute the diagram SVG carries; there is no inline script anywhere.
CSP = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    )
)

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES

# The session cookie holds nothing but a random id, and exists so that one
# browser tab's spending can be told apart from another's. Set
# ADVISOR_SECRET_KEY to keep sessions across a restart; without it they are
# reissued, which costs nothing but a reset of that tab's rate limit.
app.secret_key = os.environ.get("ADVISOR_SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")


def _session_key() -> str:
    """Which browser tab this is, for rate-limiting purposes."""
    key = session.get("id")
    if not key:
        key = session["id"] = secrets.token_urlsafe(12)
    return key


limiter = Limiter(
    key_func=_session_key,
    app=app,
    # In-memory: one process, one machine, and a limit that resets when the
    # server restarts is no worse than the ceiling it sits in front of.
    storage_uri="memory://",
    strategy="fixed-window",
)

_readme_cache: tuple[int, str] | None = None


def _error_body(error: AdvisorError) -> dict:
    """One failure, in the shape the front end reads whichever way it arrives."""
    body: dict[str, Any] = {"error": str(error), "kind": getattr(error, "kind", "error")}
    # A call that failed after the API had already done the work still costs
    # money, so hand the usage back rather than losing it from the running total.
    usage = getattr(error, "usage", None)
    if usage:
        body["usage"] = usage
    retry_after = getattr(error, "retry_after", None)
    if retry_after:
        body["retryAfter"] = retry_after
    return body


def _fail(error: AdvisorError, status: int = 502):
    return jsonify(_error_body(error)), status


def _as_recommendation(text: str) -> dict | None:
    """Read a stored reply as a recommendation, or None if it is prose.

    Conversations saved before structured output hold Markdown here, and so does
    every follow-up answer. Both fall through to the prose renderer.
    """
    stripped = text.lstrip()
    if not stripped.startswith("{"):
        return None
    try:
        data = json.loads(stripped)
    except ValueError:
        return None
    if isinstance(data, dict) and "headline" in data and "services" in data:
        return data
    return None


# Sixteen, not sixty-four. The key is the whole text of a reply, up to
# MAX_MESSAGE_CHARS of it, and the value is a rendered payload of about the same
# size -- so the cache is bounded in entries and unbounded in bytes, and 64 of
# them is a few megabytes of strings that nothing evicts. What it exists for is
# re-rendering the replies of one reopened conversation, and MAX_MESSAGES-worth
# of architectures is far fewer than sixteen.
_PAYLOAD_CACHE = 16


@lru_cache(maxsize=_PAYLOAD_CACHE)
def _payload_for(text: str) -> dict:
    """Render one reply into the fields the front end draws.

    Cached because reopening a saved conversation re-renders every reply in it.
    Callers only ever read the result, or shallow-copy it into a response.
    """
    data = _as_recommendation(text)
    if data is not None:
        return {"raw": text, **recommendation_payload(data)}
    diagram, prose = extract_mermaid(text)
    return {"raw": text, **prose_payload(prose, diagram)}


def _total_chars(messages: Sequence[Mapping[str, Any]]) -> int:
    """How much text a conversation carries, for the size guards below."""
    return sum(len(message["content"]) for message in messages)


def _within_limits(messages: Sequence[Mapping[str, Any]]) -> None:
    """Refuse a conversation too big to be worth sending on, or writing to disk."""
    if len(messages) > MAX_MESSAGES:
        raise AdvisorError(
            f"This conversation is {len(messages)} messages long, and the limit is "
            f"{MAX_MESSAGES}. Save it and start a new one.",
            kind="too_long",
        )

    for message in messages:
        size = len(message["content"])
        if size > MAX_MESSAGE_CHARS:
            raise AdvisorError(
                f"One message is {size:,} characters long, and the limit is "
                f"{MAX_MESSAGE_CHARS:,}. Shorten it and try again.",
                kind="too_long",
            )

    total = _total_chars(messages)
    if total > MAX_CONVERSATION_CHARS:
        raise AdvisorError(
            f"This conversation is {total:,} characters long, and the limit is "
            f"{MAX_CONVERSATION_CHARS:,}. Save it and start a new one.",
            kind="too_long",
        )


def _asked_for(payload: dict) -> tuple[str, str]:
    """The region and compliance profile the client chose, if either (F5).

    Read from the top level of the body, beside `messages`, the way `revise` is:
    _clean_messages keeps only the role and the content of each message, so
    there is nowhere on a message to put this.

    Anything unrecognised is dropped rather than refused. These come from two
    select elements, so a value that is not in the enum means a stale tab or
    somebody with a curl command, and neither is worth a 400 when the answer
    without it is still a good answer.
    """
    region = str(payload.get("region") or "").strip()
    compliance = str(payload.get("compliance") or "").strip()
    return (
        region if region in KNOWN_REGIONS else "",
        compliance if compliance in set(Compliance) else "",
    )


def _clean_messages(payload: dict, require_question: bool = True) -> list[MessageParam]:
    """Validate and size-check the conversation a client sent us.

    `require_question` is what separates asking from saving: the API needs the
    thread to end on a user turn, while a saved conversation ends on a reply.

    Requiring JSON is what stands in for a CSRF token here, and it is a decision
    rather than an accident. Every endpoint that changes something reads its body
    with `request.get_json`, which needs a JSON content type; a cross-origin
    request carrying one needs a preflight this server never answers, and
    SESSION_COOKIE_SAMESITE="Lax" closes what is left. So a page on another
    origin cannot make this one delete a conversation or spend money, even though
    there is no token and no authentication (S3).

    What that buys is conditional on the content type staying required.
    `get_json(force=True)` anywhere, or an endpoint that reads `request.form`,
    silently gives it up -- so don't, and see the test that asserts a
    form-encoded post is refused.
    """
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise AdvisorError("No conversation was sent.", kind="empty")

    cleaned: list[MessageParam] = []
    for message in messages:
        role = message.get("role") if isinstance(message, dict) else None
        content = str(message.get("content") or "").strip() if isinstance(message, dict) else ""
        if role not in ("user", "assistant") or not content:
            raise AdvisorError("The conversation contains an empty message.", kind="empty")
        cleaned.append({"role": role, "content": content})

    if require_question and cleaned[-1]["role"] != "user":
        raise AdvisorError("The last message must be a question.", kind="empty")

    _within_limits(cleaned)
    return cleaned


def _client_usage(payload: dict) -> dict | None:
    """Read a usage total off a request body, keeping only the fields we wrote.

    The running total lives in the browser, so what comes back is whatever the
    client says; anything that is not a number is dropped rather than saved.
    """
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    try:
        return merge_usage(usage)
    except (TypeError, ValueError):
        return None


def _daily_spend_left() -> None:
    """Refuse the call if today's token ceiling is already used up (S4).

    Refused here rather than at the API, so what comes back is a sentence about
    a budget rather than whatever the provider says when a quota runs out.
    """
    if not DAILY_TOKEN_CEILING:
        return

    spent = store.spent_on()
    if spent["tokens"] < DAILY_TOKEN_CEILING:
        return

    log.warning("daily ceiling reached: %s tokens, $%.4f", spent["tokens"], spent["costUsd"])
    raise AdvisorError(
        f"Today's ceiling of {DAILY_TOKEN_CEILING:,} tokens is used up "
        f"({spent['tokens']:,} tokens across {spent['calls']} calls, "
        f"${spent['costUsd']:.2f}). It resets at midnight, UK time. Raise "
        "ADVISOR_DAILY_TOKENS if this is the wrong number.",
        kind="rate_limit",
    )


def _record(usage: dict | None, kind: str) -> None:
    """Write one call into the ledger. A failure here must not fail the reply."""
    try:
        store.record_call(usage, kind)
    except Exception:
        log.exception("could not record what a %s call cost", kind)


def _paid_call(kind: str, make_call: Callable[[], Reply]) -> Reply:
    """Make one API call and write down what it cost, whatever happens next.

    Recorded here, in the thread that made the call, rather than where the reply
    is handed to the browser. A browser that navigates away mid-reply closes the
    generator feeding it at its next yield, and anything below that yield never
    runs -- so a ledger written there would miss exactly the abandoned calls the
    daily ceiling exists to catch. They are billed like any other.
    """
    try:
        reply = make_call()
    except AdvisorError as e:
        _record(getattr(e, "usage", None), kind)
        raise
    _record(reply.usage, kind)
    return reply


# --------------------------------------------------------------------------- #
# Front end
# --------------------------------------------------------------------------- #


@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.get("/brand.css")
def brand_css():
    """The palette, generated from branding.py on every request.

    It is not a file in static/ on purpose: branding.py is the one place the
    colours are written down, and generating the stylesheet from it is what
    keeps the app, the diagram and the exported PDF from drifting apart. No
    cache header, so editing branding.py and refreshing is the whole loop.
    """
    return Response(branding.css_root(), mimetype="text/css")


@app.after_request
def add_headers(response):
    if request.path.startswith("/assets/"):
        response.headers["Cache-Control"] = f"public, max-age={ASSET_MAX_AGE}"

    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.errorhandler(RateLimitExceeded)
def too_many(error: RateLimitExceeded):
    """Answer a rate limit in the shape the front end already understands."""
    # `error.limit` is the limiter's own object and its repr is a page long;
    # `error.limit.limit` is the rule itself, which reads as "10 per 1 minute".
    rule = getattr(error.limit, "limit", None)
    wait = getattr(getattr(rule, "GRANULARITY", None), "seconds", None)
    log.warning("rate limited: %s on %s", rule, request.path)
    return _fail(
        AdvisorError(
            f"That is more requests than this tool expects ({rule}). "
            + (f"Try again in up to {wait} seconds." if wait else "Try again shortly."),
            kind="rate_limit",
            retry_after=wait,
        ),
        429,
    )


@app.errorhandler(413)
def too_large(_error):
    """Answer an oversized body in the shape the front end already understands."""
    return _fail(
        AdvisorError(
            f"That request was larger than the {MAX_BODY_BYTES // 1024} KB limit.",
            kind="too_long",
        ),
        413,
    )


@app.get("/api/health")
def health():
    """Tell the front end up front whether the API key is usable.

    This used to return `getpass.getuser()` as well, to draw an initial in the
    sidebar avatar. Harmless on one person's laptop, an information disclosure
    the moment the app is bound to anything else, and worth nothing either way
    while there is no authentication to get a real identity from (S3).
    """
    spent = store.spent_on()
    budget = {
        "ceiling": DAILY_TOKEN_CEILING,
        "tokens": spent["tokens"],
        "costUsd": spent["costUsd"],
    }
    # F3: whether report.pdf is on the menu, which depends on a browser being
    # installed rather than on anything the app can fix at run time.
    exports = {"pdf": bool(export.chrome())}
    try:
        get_client()
    except AdvisorError as e:
        return jsonify(
            {
                "ok": False,
                "error": str(e),
                "kind": e.kind,
                "model": MODEL,
                "budget": budget,
                "exports": exports,
            }
        )
    return jsonify({"ok": True, "model": MODEL, "budget": budget, "exports": exports})


@app.get("/api/readme")
def readme():
    """Back the 'Setup guide' link with the project's own README."""
    global _readme_cache
    try:
        stamp = README.stat().st_mtime_ns
    except OSError:
        return jsonify({"html": "<p>No README.md found in the project root.</p>"})

    if _readme_cache is None or _readme_cache[0] != stamp:
        _readme_cache = (stamp, md_to_html(README.read_text(encoding="utf-8")))
    return jsonify({"html": _readme_cache[1]})


# --------------------------------------------------------------------------- #
# Streaming
#
# Both API calls stream. A first recommendation takes around twenty seconds, and
# what goes down the wire is the recommendation itself, field by field as it is
# written: headline first, then the services, then the cost, then the diagram.
# The browser draws each snapshot as it arrives. Claude's summarised reasoning
# is sent too, for the seconds before any of the answer exists, though on Sonnet
# 5 there is usually little or none of it.
#
# Server-Sent Events rather than a WebSocket: this is one-way, it is a plain
# POST, and it survives a proxy that knows nothing about it.
# --------------------------------------------------------------------------- #

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    # nginx buffers responses by default, which would hold the whole stream back
    # until it finished and undo the point of the exercise.
    "X-Accel-Buffering": "no",
}
# Note the header that is deliberately absent: `Connection: keep-alive`. Setting
# it by hand on a streamed response tells the browser to hold the socket open
# after the stream ends, and a browser only allows six per origin, so after six
# replies every later request queues behind a connection that will never be
# reused. Connection handling belongs to the server, not to this dict.

# One line of reasoning is enough to show the user something is happening;
# the whole of it would be a wall of text under a progress indicator.
MAX_THINKING_CHARS = 160


def _sse(event: dict) -> str:
    """One Server-Sent Event. Newlines inside the payload are escaped by json."""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _thinking_line(thinking: str) -> str:
    """The last thing Claude was thinking, short enough to sit on one line."""
    lines = [line.strip() for line in thinking.strip().split("\n") if line.strip()]
    if not lines:
        return ""
    last = lines[-1].lstrip("#* ")
    return last if len(last) <= MAX_THINKING_CHARS else last[: MAX_THINKING_CHARS - 1] + "\u2026"


def _progress_event(progress: Progress, structured: bool) -> dict | None:
    """Turn a snapshot of a half-written reply into something to draw.

    Returns None when there is nothing new worth sending -- a structured reply
    with no complete field yet, say -- and the frame is skipped.
    """
    if not progress.text.strip():
        line = _thinking_line(progress.thinking)
        return {"type": "thinking", "text": line} if line else None

    if not structured:
        return {"type": "partial", "message": prose_payload(progress.text)}

    data = partial_json(progress.text)
    if not isinstance(data, dict):
        return None
    return {"type": "partial", "message": recommendation_payload(data)}


def _run_jobs(
    jobs: Sequence[Callable[[Callable[[Progress], None]], Reply]],
) -> Iterator[tuple[str, int, Any]]:
    """Run streaming calls concurrently, yielding what they report as it happens.

    Each job is handed a callback to report progress through. The generator
    yields ("partial", index, Progress) as they go and one ("done", index,
    Reply) or ("error", index, AdvisorError) per job at the end. The queue is
    what lets several calls -- a comparison is two -- interleave into one
    response.

    If the browser goes away mid-stream the generator is closed and the worker
    threads run on to completion with nobody reading; they cannot be cancelled
    once the API call is in flight, and the reply is simply dropped.
    """
    reports: queue.Queue[tuple[str, int, Any]] = queue.Queue()

    def run(index: int, job: Callable[[Callable[[Progress], None]], Reply]) -> None:
        try:
            reply = job(lambda progress: reports.put(("partial", index, progress)))
            reports.put(("done", index, reply))
        except AdvisorError as e:
            reports.put(("error", index, e))
        except Exception:
            # A bug here would otherwise hang the browser on a stream that never
            # ends, so it becomes an ordinary failure with the detail in the log.
            log.exception("streaming call %s failed", index)
            reports.put(
                ("error", index, AdvisorError("Something went wrong on the server.", kind="error"))
            )

    # A thread each, rather than a pool. A browser that goes away mid-reply
    # leaves its call running -- an API call in flight cannot be cancelled --
    # and with a small fixed pool a handful of those would queue everything
    # behind them until they finished. What bounds how many can be in flight is
    # the rate limiter above, not the size of a pool.
    for index, job in enumerate(jobs):
        worker = threading.Thread(
            target=run, args=(index, job), name=f"advisor-{index}", daemon=True
        )
        worker.start()

    outstanding = len(jobs)
    while outstanding:
        outcome, index, value = reports.get()
        if outcome != "partial":
            outstanding -= 1
        yield outcome, index, value


def _stream(events: Iterator[str]) -> Response:
    return Response(events, mimetype="text/event-stream", headers=SSE_HEADERS)


@app.post("/api/advise")
@limiter.limit(SESSION_LIMITS)
@limiter.limit(IP_LIMITS, key_func=get_remote_address)
def api_advise():
    """Continue a conversation. The first reply carries the full recommendation."""
    payload = request.get_json(silent=True) or {}
    try:
        messages = _clean_messages(payload)
        client = get_client()
        _daily_spend_left()
    except AdvisorError as e:
        if e.kind == "rate_limit":
            return _fail(e, 429)
        return _fail(e, 400 if e.kind in ("empty", "too_long", "missing_key") else 502)

    first = not any(message["role"] == "assistant" for message in messages)
    # `revise` comes from the button, not from reading what the user typed. The
    # alternative is classifying every follow-up, and an architecture that
    # changed because somebody asked what an ALB was is worse than one that
    # never changes. It only means anything once there is an architecture to
    # revise, so the first turn is a recommendation whatever the client asked.
    revising = bool(payload.get("revise")) and not first
    kind: Kind = "recommendation" if first else "revision" if revising else "follow_up"
    structured = kind in STRUCTURED_KINDS
    constraints = constraints_line(*_asked_for(payload))

    if _total_chars(messages) > PREFLIGHT_CHARS:
        counted = count_input_tokens(client, messages, kind)
        if counted > MAX_INPUT_TOKENS:
            return _fail(
                AdvisorError(
                    f"This conversation is {counted:,} tokens of input, and the limit is "
                    f"{MAX_INPUT_TOKENS:,}. Save it and start a new one.",
                    kind="too_long",
                ),
                400,
            )

    # A5: the server names a conversation, so the rule for doing it exists once.
    title = store.title_from(str(next(m["content"] for m in messages if m["role"] == "user")))

    def job(report: Callable[[Progress], None]) -> Reply:
        return _paid_call(kind, lambda: call_claude(client, messages, kind, report, constraints))

    def events() -> Iterator[str]:
        for outcome, _, value in _run_jobs([job]):
            if outcome == "partial":
                # `structured` rather than `first`: a revision is the second
                # architecture in a thread, and drawing its JSON as prose would
                # stream the schema at the user a field at a time.
                frame = _progress_event(value, structured)
                if frame:
                    yield _sse(frame)
            elif outcome == "done":
                yield _sse(
                    {
                        "type": "done",
                        "message": _payload_for(value.text),
                        "usage": value.usage,
                        "title": title,
                    }
                )
            else:
                yield _sse({"type": "error", **_error_body(value)})

    return _stream(events())


def _compare_cost() -> int:
    """What a comparison counts against the rate limit: one per architecture.

    A callable rather than a fixed 2, because a comparison is however many
    workloads were sent (F9). Four architectures is four calls, and a limit that
    charged for two would let a runaway tab spend at twice the rate it was
    written to allow.

    Read straight off the body rather than from anything the view has computed,
    because flask-limiter calls this before the view runs. Clamped, so a
    malformed body cannot ask for a cost of nothing or of a thousand.
    """
    payload = request.get_json(silent=True) or {}
    asked = len(payload.get("workloads") or []) if isinstance(payload, dict) else 0
    return max(1, min(asked, MAX_COMPARE))


@app.post("/api/compare")
# One call per architecture, so a comparison counts as its own width.
@limiter.limit(SESSION_LIMITS, cost=_compare_cost)
@limiter.limit(IP_LIMITS, key_func=get_remote_address, cost=_compare_cost)
def api_compare():
    """Several workloads, an architecture each, requested concurrently."""
    payload = request.get_json(silent=True) or {}
    workloads = [str(item or "").strip() for item in (payload.get("workloads") or [])]

    if not MIN_COMPARE <= len(workloads) <= MAX_COMPARE or not all(workloads):
        return _fail(
            AdvisorError(
                f"Describe between {MIN_COMPARE} and {MAX_COMPARE} workloads before "
                "comparing. An empty field leaves nothing to weigh up.",
                kind="empty",
            ),
            400,
        )

    try:
        _within_limits([{"content": workload} for workload in workloads])
        client = get_client()
        _daily_spend_left()
    except AdvisorError as e:
        return _fail(e, 429 if e.kind == "rate_limit" else 400)

    # Every option is built to the same constraints: comparing a London
    # architecture against an Oregon one would not be a comparison (F5).
    constraints = constraints_line(*_asked_for(payload))

    def make_job(workload: str) -> Callable[[Callable[[Progress], None]], Reply]:
        return lambda report: _paid_call(
            "comparison", lambda: advise(client, workload, "comparison", report, constraints)
        )

    def events() -> Iterator[str]:
        results: list[dict | None] = [None] * len(workloads)
        usages: list[dict | None] = []
        failure: AdvisorError | None = None

        for outcome, index, value in _run_jobs([make_job(text) for text in workloads]):
            if outcome == "partial":
                frame = _progress_event(value, True)
                if frame:
                    yield _sse({**frame, "index": index})
            elif outcome == "done":
                results[index] = _payload_for(value.text)
                usages.append(value.usage)
                yield _sse({"type": "partial", "index": index, "message": results[index]})
            else:
                # Every column is still waited for: one failing does not mean
                # the others' tokens should go unaccounted for.
                failure = failure or value
                usages.append(getattr(value, "usage", None))

        usage = merge_usage(*usages)
        if failure or not all(results):
            error = failure or AdvisorError("One of the architectures did not come back.")
            yield _sse({"type": "error", **_error_body(error), "usage": usage})
        else:
            yield _sse(
                {
                    "type": "done",
                    "results": results,
                    "usage": usage,
                    "title": f"Compare: {store.title_from(workloads[0])}",
                }
            )

    return _stream(events())


@app.post("/api/estimate")
@limiter.limit(ESTIMATE_LIMITS, key_func=get_remote_address)
def api_estimate():
    """Price a recommendation against the AWS Price List (F1).

    Its own endpoint, rather than part of the reply, for two reasons: the first
    architecture in a region waits twenty seconds on AWS's own files while
    later ones wait for nothing, and a recommendation that cannot be priced is
    still a recommendation. The browser asks for this once the reply is on
    screen, and fills the figure in when it arrives.
    """
    payload = request.get_json(silent=True) or {}
    raw = payload.get("raw")
    if not isinstance(raw, str) or not raw.strip():
        return _fail(AdvisorError("There is no recommendation to price.", kind="empty"), 400)

    try:
        recommendation = Recommendation.model_validate_json(raw)
    except ValueError:
        return _fail(
            AdvisorError("That is not a recommendation this can price.", kind="empty"), 400
        )

    try:
        estimate = pricing.estimate(recommendation)
    except Exception:
        # AWS being unreachable, or a price list that has changed shape. The
        # recommendation is unaffected, so this is a missing figure rather than
        # a failed request.
        log.exception("could not price a recommendation")
        return _fail(
            AdvisorError(
                "The AWS Price List could not be read, so this is unpriced.", kind="pricing"
            ),
            502,
        )

    return jsonify({"estimate": estimate})


@app.post("/api/save")
def api_save():
    """Store the conversation and report back which one it became."""
    payload = request.get_json(silent=True) or {}
    mode = "compare" if payload.get("mode") == "compare" else "advise"

    if not isinstance(payload.get("messages"), list) or not payload["messages"]:
        return _fail(AdvisorError("There is nothing to save yet.", kind="empty"), 400)

    try:
        messages = _clean_messages(payload, require_question=False)
    except AdvisorError as e:
        return _fail(e, 400)

    title = store.clean_title(payload.get("title"))
    # The region is read back off the reply, so only the compliance profile has
    # to be carried: nothing in the architecture records what it was built to.
    _, compliance = _asked_for(payload)
    try:
        conversation_id = store.save(
            messages,
            mode=mode,
            title=title,
            usage=_client_usage(payload),
            compliance=compliance,
        )
    except (AdvisorError, sqlite3.Error) as e:
        error = e if isinstance(e, AdvisorError) else AdvisorError(f"Could not save: {e}.")
        return _fail(error, 500)

    first = next((message["content"] for message in messages if message["role"] == "user"), "")
    return jsonify({"id": conversation_id, "title": title or store.title_from(str(first))})


# --------------------------------------------------------------------------- #
# Saved conversations
# --------------------------------------------------------------------------- #


@app.get("/api/conversations")
def list_conversations():
    """Everything saved, newest first.

    One query now, where this used to open and parse every file in a directory
    and keep a cache keyed on mtime to make that bearable.
    """
    return jsonify({"conversations": store.listing()})


@app.get("/api/conversations/<int:conversation_id>")
def read_conversation(conversation_id: int):
    """Load a saved conversation, re-rendered so it looks as it first did."""
    conversation = store.read(conversation_id)
    if conversation is None:
        return _fail(AdvisorError("That conversation is not here.", kind="not_found"), 404)

    messages = []
    for message in conversation["messages"]:
        if message["role"] == "assistant":
            messages.append({"role": "assistant", **_payload_for(message["content"])})
        else:
            messages.append({"role": "user", "content": message["content"]})

    return jsonify({**conversation, "messages": messages})


@app.get("/api/conversations/<int:conversation_id>/export")
def export_conversation(conversation_id: int):
    """The same JSON document the old file store wrote, as a download.

    Storage moved to SQLite; the format did not have to, and anything that read
    those files still can.
    """
    payload = store.export_json(conversation_id)
    if payload is None:
        return _fail(AdvisorError("That conversation is not here.", kind="not_found"), 404)

    stamp = payload["timestamp"][:19].replace("-", "").replace(":", "").replace("T", "_")
    response = jsonify(payload)
    response.headers["Content-Disposition"] = (
        f'attachment; filename="conversation_{stamp}-{conversation_id}.json"'
    )
    return response


@app.delete("/api/conversations/<int:conversation_id>")
def delete_conversation(conversation_id: int):
    """Delete a saved conversation."""
    if not store.delete(conversation_id):
        return _fail(AdvisorError("That conversation is not here.", kind="not_found"), 404)
    return jsonify({"id": conversation_id, "deleted": True})


@app.post("/api/conversations/<int:conversation_id>/title")
def rename_conversation(conversation_id: int):
    """Rename a saved conversation."""
    asked = (request.get_json(silent=True) or {}).get("title")
    try:
        title = store.rename(conversation_id, asked)
    except AdvisorError as e:
        return _fail(e, 400)
    if title is None:
        return _fail(AdvisorError("That conversation is not here.", kind="not_found"), 404)
    return jsonify({"id": conversation_id, "title": title})


# --------------------------------------------------------------------------- #
# Finding one again (F8)
# --------------------------------------------------------------------------- #
#
# No rate limit and no ledger entry on any of these. They cost no Anthropic
# money, which is what the limits above exist to bound, and reading has never
# been limited here -- see the test that says so. What keeps a search-as-you-type
# box from hammering the disk is the debounce in app.js, which is the right place
# for it.


@app.get("/api/search")
def search_conversations():
    """Saved conversations matching a query, a facet, or both."""
    args = request.args
    try:
        found = store.search(
            text=args.get("q", ""),
            tier=args.get("tier", ""),
            service=args.get("service", ""),
            tag=args.get("tag", ""),
            region=args.get("region", ""),
        )
    except sqlite3.Error as e:
        # A filter is user input reaching SQL, so a malformed one is a bad
        # request rather than a page that breaks.
        log.warning("could not search: %s", e)
        return _fail(AdvisorError("That search could not be run.", kind="empty"), 400)
    return jsonify({"conversations": found, "tags": store.all_tags()})


@app.get("/api/usage")
def usage_report():
    """What a month of advice cost, by day and by kind (F14).

    Unlimited and unledgered like the rest of this section. It is the one place
    the daily ceiling is visible before it refuses something, so it carries the
    ceiling as well as the spend.
    """
    try:
        report = store.spend_report(request.args.get("month", ""))
    except ValueError:
        # A month arrives as a query parameter, so a bad one is a bad request
        # rather than a page that breaks.
        return _fail(AdvisorError("That is not a month this can report on.", kind="empty"), 400)
    except sqlite3.Error as e:
        log.warning("could not read the ledger: %s", e)
        return _fail(AdvisorError("The spend report could not be built.", kind="empty"), 400)
    return jsonify({**report, "ceiling": DAILY_TOKEN_CEILING})


@app.post("/api/conversations/<int:conversation_id>/tags")
def tag_conversation(conversation_id: int):
    """Tag a saved conversation, or untag it with {"remove": true}."""
    payload = request.get_json(silent=True) or {}
    asked = payload.get("tag")
    try:
        if payload.get("remove"):
            tags = store.remove_tag(conversation_id, asked)
        else:
            tags = store.add_tag(conversation_id, asked)
    except AdvisorError as e:
        return _fail(e, 400)
    except sqlite3.Error as e:
        return _fail(AdvisorError(f"Could not tag that: {e}."), 500)
    if tags is None:
        return _fail(AdvisorError("That conversation is not here.", kind="not_found"), 404)
    return jsonify({"id": conversation_id, "tags": tags})


# --------------------------------------------------------------------------- #
# Exporting a deliverable (F3)
# --------------------------------------------------------------------------- #

# An export costs no Anthropic money, but it does start a browser to print the
# PDF, which is the most expensive thing this server does per request. Its own
# allowance, tighter than the estimate's, because nobody needs six deliverables
# a minute and a loop that produced them would spawn six browsers.
EXPORT_LIMITS = "6 per minute; 60 per hour"

# What a client may send as a cached estimate. Names and details are escaped
# wherever they land, but they are still capped here: a document is not the place
# to discover that someone posted a megabyte of service name.
MAX_ESTIMATE_SERVICES = 40
MAX_ESTIMATE_LINES = 12
MAX_ESTIMATE_TEXT = 200


def _number(value: Any) -> float:
    """A figure from a request body, or zero if it is not one."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number == number and abs(number) != float("inf") else 0.0


def _clean_estimate(raw: Any) -> dict[str, Any] | None:
    """Take a priced estimate off a request body, keeping only what we wrote.

    The figures on screen came from /api/estimate, and the browser holds them so
    that exporting does not re-read a 200 MB price list. What comes back is still
    a request body, so this keeps the fields export.py reads, coerces every
    figure to a number and caps every string, rather than trusting the shape.

    The gate is `hasFigure` rather than `priced` (F12): an architecture the price
    list covers none of still has a monthly figure, made of the advisor's own
    estimates, and refusing to export it would lose the only number there is.
    """
    if not isinstance(raw, dict) or not (raw.get("hasFigure") or raw.get("priced")):
        return None

    # Annotated: left to infer from the append below, this is `dict[str, object]`
    # and `service["lines"]` is not something mypy will let anything iterate.
    services: list[dict[str, Any]] = []
    for service in (raw.get("services") or [])[:MAX_ESTIMATE_SERVICES]:
        if not isinstance(service, dict):
            continue
        lines = [
            {
                "meter": str(line.get("meter") or "")[:MAX_ESTIMATE_TEXT],
                "priced": bool(line.get("priced")),
                "estimated": bool(line.get("estimated")),
                "monthlyUsd": _number(line.get("monthlyUsd")),
                "detail": str(line.get("detail") or "")[:MAX_ESTIMATE_TEXT],
            }
            for line in (service.get("lines") or [])[:MAX_ESTIMATE_LINES]
            if isinstance(line, dict)
        ]
        services.append(
            {
                "name": str(service.get("name") or "")[:MAX_ESTIMATE_TEXT],
                "monthlyUsd": _number(service.get("monthlyUsd")),
                "priced": bool(service.get("priced")),
                "estimated": bool(service.get("estimated")),
                "lines": lines,
            }
        )

    if not services:
        return None

    # Recomputed from the cleaned services rather than read off the body, the
    # way `priced` always has been: these three decide what the document says
    # about its own figures, and a client that gets them wrong would have the
    # document contradict its own table.
    monthly = _number(raw.get("monthlyUsd"))
    priced_usd = _number(raw.get("pricedUsd"))
    estimated_usd = _number(raw.get("estimatedUsd"))
    lines = [line for service in services for line in service["lines"]]

    return {
        "region": str(raw.get("region") or "")[:32],
        "monthlyUsd": monthly,
        "pricedUsd": priced_usd,
        "estimatedUsd": estimated_usd,
        "tier": str(raw.get("tier") or "")[:32],
        "claimedTier": str(raw.get("claimedTier") or "")[:32],
        "tierGap": max(0, min(4, int(_number(raw.get("tierGap"))))),
        "priced": any(service["priced"] for service in services),
        "anyEstimated": any(service["estimated"] for service in services),
        "hasFigure": monthly > 0,
        "unpricedServices": min(len(services), int(_number(raw.get("unpricedServices")))),
        "estimatedServices": sum(1 for service in services if service["estimated"]),
        "linesPriced": sum(1 for line in lines if line["priced"]),
        "linesEstimated": sum(1 for line in lines if line["estimated"]),
        "linesTotal": len(lines),
        "pricedAt": str(raw.get("pricedAt") or "")[:40],
        "services": services,
    }


def _for_export(data: Mapping[str, Any]) -> tuple[Recommendation, str] | None:
    """Read a stored recommendation, tolerating the fields the schema has gained.

    Returns (recommendation, region), where region is "" for a review that never
    stated one, or None if the reply is not a recommendation this can read.

    Everything saved before the pricing work (F1) is missing the top-level
    `region` and the per-service `usage` the AWS Price List is queried with, and
    everything saved before F12 is missing `estimated_monthly_usd` on each line
    of that usage. None of them changes a word of the document -- usage is only
    ever priced, and the estimate carries its own region -- so validating
    strictly against today's schema would refuse to export a conversation for
    the sake of fields the document does not print. They are filled in here.

    A missing estimate fills as zero rather than a guess, which is the one
    honest value: a reply written before the field existed said nothing about
    what these lines cost, and pricing.py renders a zero estimate as no figure
    rather than as free.

    The region is the one that cannot be filled in: nobody chose one, so the
    document says nothing rather than naming London on the model's behalf. The
    value handed to the schema is only what pydantic needs to build the object;
    what the document prints is the empty string returned beside it.
    """
    patched = dict(data)
    patched["services"] = [
        {
            **service,
            "usage": [
                {"estimated_monthly_usd": 0.0, **line} if isinstance(line, Mapping) else line
                for line in service.get("usage") or []
            ],
        }
        for service in patched.get("services") or []
        if isinstance(service, Mapping)
    ]

    stated = str(patched.get("region") or "")
    if stated not in KNOWN_REGIONS:
        patched["region"] = Region.LONDON.value
        stated = ""

    try:
        recommendation = Recommendation.model_validate(patched)
    except ValueError as error:
        # Worth a line in the log: the alternative is a user being told there is
        # nothing to export with no way to find out why.
        log.warning("a stored reply could not be read as a recommendation: %s", error)
        return None
    return recommendation, stated


def _options(
    messages: Sequence[Mapping[str, Any]], estimates: Any, compare: bool = False
) -> list[export.Option]:
    """The architectures in a conversation, in the order they were given.

    A recommendation is any assistant turn that parses as one: the first reply in
    a thread, both replies in a comparison, and any follow-up that answered with a
    revised architecture. The user turn before it is the brief that produced it,
    which is what a client recognises on a cover.

    `compare` is what the browser says the conversation is, and it decides how
    many come back. Without it, a thread whose follow-up revised the architecture
    would be documented as "Option A" and "Option B" -- two choices to weigh,
    when what happened was one being replaced by the other.
    """
    priced = estimates if isinstance(estimates, list) else []
    options: list[export.Option] = []
    brief = ""

    for message in messages:
        if message["role"] == "user":
            # The first question after a reply is the next option's brief; a
            # follow-up in the same thread is not, and is overwritten by nothing.
            brief = brief or str(message["content"])
            continue
        data = _as_recommendation(str(message["content"]))
        if data is None:
            continue
        read = _for_export(data)
        if read is None:
            continue
        recommendation, region = read
        index = len(options)
        options.append(
            export.Option(
                recommendation=recommendation,
                estimate=_clean_estimate(priced[index] if index < len(priced) else None),
                brief=brief,
                label="" if index == 0 else f"Option {chr(ord('A') + index)}",
                stated_region=region,
            )
        )
        # In a comparison the next user turn is the second option's own brief.
        brief = ""

    if not compare:
        if not options:
            return []
        # The most recent architecture is the advice as it now stands, and the
        # brief is still the question the thread opened with: a revision does not
        # change what was asked for.
        opening = next((str(message["content"]) for message in messages), "")
        return [replace(options[-1], brief=opening, label="")]

    return [
        replace(option, label=f"Option {chr(ord('A') + index)}")
        for index, option in enumerate(options[:MAX_COMPARE])
    ]


def _deliverable(payload: dict) -> export.Deliverable:
    """Build the document model from what the browser sent."""
    messages = _clean_messages(payload, require_question=False)
    compare = str(payload.get("mode") or "advise") == "compare"
    options = _options(messages, payload.get("estimates"), compare)
    if not options:
        raise AdvisorError(
            "There is no reviewed architecture in this conversation to export yet.",
            kind="empty",
        )

    transcript = [(str(message["role"]), str(message["content"])) for message in messages]
    return export.Deliverable(
        options=options[:MAX_COMPARE],
        meta=export.Meta.from_request(payload),
        transcript=transcript,
        model=MODEL,
    )


def _formats(payload: dict) -> list[str]:
    """Which files were asked for, in a fixed order and with nothing invented."""
    asked = payload.get("formats")
    if not isinstance(asked, list):
        raise AdvisorError("No format was chosen, so there is nothing to export.", kind="empty")
    chosen = [name for name in export.FORMATS if name in {str(item) for item in asked}]
    if not chosen:
        raise AdvisorError("No format was chosen, so there is nothing to export.", kind="empty")
    return chosen


@app.post("/api/export")
@limiter.limit(EXPORT_LIMITS)
@limiter.limit(EXPORT_LIMITS, key_func=get_remote_address)
def api_export():
    """Write the conversation out as a client-ready deliverable (F3).

    Returns the file itself rather than a link to one: nothing is kept on the
    server, so there is no temporary directory to clean up and no URL that
    outlives the download. A format that could not be written -- a PDF with no
    browser to print it -- comes back in the X-Export-Note header, because the
    body is a file by then and the other formats are still worth having.
    """
    payload = request.get_json(silent=True) or {}
    try:
        formats = _formats(payload)
        deliverable = _deliverable(payload)
    except AdvisorError as e:
        return _fail(e, 400)

    session_json = None
    if "json" in formats:
        # The same document /api/conversations/<id>/export writes, so a
        # deliverable and a saved conversation carry the same session file.
        session_json = json.dumps(
            {
                "timestamp": datetime.now(store.UK).isoformat(),
                "version": store.SAVE_VERSION,
                "model": MODEL,
                "mode": "compare" if len(deliverable.options) > 1 else "advise",
                "messages": [
                    {"role": role, "content": content} for role, content in deliverable.transcript
                ],
            },
            ensure_ascii=False,
            indent=2,
        )

    try:
        written, problems = export.bundle(deliverable, formats, session_json)
    except export.ExportError as e:
        log.warning("export failed: %s", e)
        return _fail(AdvisorError(str(e), kind=e.kind), 502)
    except Exception:
        log.exception("could not write a deliverable")
        return _fail(AdvisorError("The deliverable could not be written.", kind="export"), 500)

    log.info(
        "exported %s (%s) as %s, %s bytes",
        written.name,
        "+".join(formats),
        written.mime,
        len(written.body),
    )

    response = Response(written.body, mimetype=written.mime.split(";")[0])
    response.headers["Content-Disposition"] = f'attachment; filename="{written.name}"'
    response.headers["Content-Length"] = str(len(written.body))
    # Nothing about a deliverable should be cached: the next one differs.
    response.headers["Cache-Control"] = "no-store"
    if problems:
        # Headers are latin-1 on the wire, and these sentences are ASCII, but
        # encode explicitly rather than trusting that to stay true.
        note = " ".join(problems).encode("ascii", "replace").decode("ascii")
        response.headers["X-Export-Note"] = note[:400]
    return response


if __name__ == "__main__":
    # Debug mode is opt-in: it enables the Werkzeug debugger, which will run
    # arbitrary code from the browser, and the reloader, which drops in-flight
    # requests when it restarts. Set ADVISOR_DEBUG=1 while working on the app.
    debug = os.environ.get("ADVISOR_DEBUG") == "1"
    port = int(os.environ.get("ADVISOR_PORT", "5000"))

    # Werkzeug's access log says a request happened; this says what the request
    # cost, how long it waited on the API, and which Anthropic request id to
    # quote if it went wrong. ADVISOR_LOG=DEBUG turns the SDK's own log on too.
    level = os.environ.get("ADVISOR_LOG", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    # Pull the default region's prices in the background, so the first
    # architecture of the day is priced instantly rather than waiting on a
    # 200 MB download. Skipped entirely when they are already fresh.
    threading.Thread(
        target=pricing.warm, args=(Region.LONDON.value,), name="advisor-prices", daemon=True
    ).start()

    print(f"{branding.BRAND_NAME} {branding.PRODUCT_NAME} -> http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=debug, threaded=True)
