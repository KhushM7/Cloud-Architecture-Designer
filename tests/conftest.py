"""Shared fixtures.

Nothing in the suite talks to the Claude API. The two ways in are `call_claude`
and `advise`, and every test that would reach them either passes a fake client or
patches them on the module under test.

Nothing in the default run reaches the network either -- see no_outbound_http
below, which is what makes that claim true rather than merely intended.
"""

from __future__ import annotations

import json
import time
import urllib.request
from types import SimpleNamespace
from typing import Any

import pytest
from anthropic.types import CitationsWebSearchResultLocation, TextBlock

import advisor
import export
import server
import store
from schema import Recommendation
from tests.samples import FOLLOW_UP, FULL_JSON, REVISED_JSON

FAKE_KEY = "sk-ant-test-not-a-real-key"

# What the fake stream reports as Claude's reasoning before any answer exists.
FAKE_THINKING = "Weighing Multi-AZ against a read replica.\nChecking the cost of both."


@pytest.fixture(autouse=True)
def fake_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test a key that is obviously not real.

    `advisor` reads .env at import, so without this a developer's own key would be
    the one under test -- and on a machine without one, get_client() would fail
    for reasons that have nothing to do with the test.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    monkeypatch.setattr(advisor, "_clients", {})


@pytest.fixture(autouse=True)
def report_every_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Report progress on every delta, rather than a few times a second.

    call_claude throttles by wall clock, which a test cannot wait out without
    being slow for no reason.
    """
    monkeypatch.setattr(advisor, "PROGRESS_INTERVAL", 0.0)


BLOCKED_URL = (
    "the default test run does not reach the network, and something asked for {url}. "
    "Stub the fetch, or mark the test @pytest.mark.network."
)


@pytest.fixture(autouse=True)
def no_outbound_http(request, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the default run off the network, the way the README says it is (T1).

    It was not. `pricing.estimate` warms a region whose rates are not cached, the
    throwaway database every test gets means they never are, and eight CLI tests
    were each streaming AWS's real price list files -- the 210 MB EC2 one
    included. That was most of the suite's runtime, and it made `-m network` mean
    nothing.

    Blocked at urlopen rather than by faking `pricing.estimate`, so the pricing
    code under test stays real: `_rows` catches this as it would a genuinely
    unreachable AWS, `warm` reports it in `problems`, and the estimate falls back
    to the advisor's own figures. An OSError rather than an assertion for the same
    reason -- it is the failure being simulated, and it is the one the code
    already knows how to handle.

    A test that wants the real Price List carries @pytest.mark.network, which is
    the deal `without_a_rasteriser` strikes below for the browser.
    """
    if "network" in request.keywords:
        return

    def blocked(url: Any, *args: Any, **kwargs: Any) -> Any:
        raise OSError(BLOCKED_URL.format(url=getattr(url, "full_url", url)))

    monkeypatch.setattr(urllib.request, "urlopen", blocked)


@pytest.fixture
def conversations(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Point the store at a throwaway database."""
    path = tmp_path / "advisor.db"
    monkeypatch.setattr(store, "DB_PATH", path)
    store.reset_for_tests()
    yield path
    store.reset_for_tests()


def cited(url: str, title: str = "AWS documentation") -> CitationsWebSearchResultLocation:
    """One web search citation, as the API attaches them to a text block (F2)."""
    return CitationsWebSearchResultLocation(
        type="web_search_result_location",
        url=url,
        title=title,
        cited_text="whatever the page said",
        encrypted_index="opaque",
    )


def fake_response(
    text: str = "hello",
    stop_reason: str = "end_turn",
    input_tokens: int = 1000,
    output_tokens: int = 500,
    cache_read: int = 0,
    cache_write: int = 0,
    stop_details: Any = None,
    parsed: Any = None,
    citations: list[CitationsWebSearchResultLocation] | None = None,
    searches: int = 0,
) -> SimpleNamespace:
    """A stand-in for a Message, carrying only what call_claude reads off one.

    `citations` hang off the text block, as they do on a real searched reply, and
    `searches` is what the API says that cost. An unsearched reply is what every
    other test gets: no server tool use on the usage at all.
    """
    return SimpleNamespace(
        id="msg_fake",
        stop_reason=stop_reason,
        stop_details=stop_details,
        content=(
            [TextBlock(type="text", text=text, citations=citations or None)]
            if text is not None
            else []
        ),
        parsed_output=parsed,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
            server_tool_use=(
                SimpleNamespace(web_search_requests=searches, web_fetch_requests=0)
                if searches
                else None
            ),
        ),
    )


def _delta(kind: str, value: str) -> SimpleNamespace:
    """One content_block_delta event, in the shape the SDK emits."""
    delta = SimpleNamespace(type=kind, **{kind.removesuffix("_delta"): value})
    return SimpleNamespace(type="content_block_delta", delta=delta)


def _chunks(text: str, parts: int = 4) -> list[str]:
    """Split a reply into a handful of deltas, as the API would."""
    size = max(1, -(-len(text) // parts))
    return [text[at : at + size] for at in range(0, len(text), size)]


def _citation_delta(citation: Any) -> SimpleNamespace:
    """One citations_delta event: how a cited page arrives while the reply streams."""
    return SimpleNamespace(
        type="content_block_delta",
        delta=SimpleNamespace(type="citations_delta", citation=citation),
    )


class FakeStream:
    """What client.messages.stream() hands back: a context manager of events."""

    def __init__(self, response: SimpleNamespace, thinking: str, cite: bool = True) -> None:
        self.response = response
        self.thinking = thinking
        # Whether the citations on the reply also arrive as deltas. A test turns
        # this off to exercise reading them off the finished message instead.
        self.cite = cite

    def __enter__(self) -> FakeStream:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def __iter__(self):
        for part in _chunks(self.thinking, 2):
            yield _delta("thinking_delta", part)
        yield SimpleNamespace(type="content_block_stop", index=0)
        for index, block in enumerate(self.response.content, start=1):
            # Citations arrive at the start of the block they belong to, which is
            # why call_claude holds their markers until the block ends.
            if self.cite:
                for citation in block.citations or []:
                    yield _citation_delta(citation)
            for part in _chunks(block.text):
                yield _delta("text_delta", part)
            # A delta type call_claude ignores, so the filtering is real.
            yield _delta("signature_delta", "ignored")
            yield SimpleNamespace(type="content_block_stop", index=index)

    def get_final_message(self) -> SimpleNamespace:
        return self.response


class FakeMessages:
    """The `client.messages` surface, recording what it was asked for."""

    def __init__(
        self,
        response: SimpleNamespace | Exception | list[SimpleNamespace],
        token_count: int | Exception = 400,
    ) -> None:
        # Either may be an exception instead, which is how a test makes the call
        # it stands in for fail. A list is a turn that takes more than one call --
        # a paused search being handed back -- and its last entry stands for every
        # call after it, which is a turn that never stops pausing.
        self.response = response
        self.token_count = token_count
        self.created: list[dict[str, Any]] = []
        self.counted: list[dict[str, Any]] = []
        self.thinking = FAKE_THINKING
        self.cite = True

    def _next(self) -> SimpleNamespace:
        # Narrowed for the type checker as well as the reader: stream() raises a
        # bare Exception before it gets here, so by this point `response` is
        # either one namespace or a list of them.
        if isinstance(self.response, Exception):
            raise self.response
        if not isinstance(self.response, list):
            return self.response
        return self.response[min(len(self.created), len(self.response)) - 1]

    def stream(self, **kwargs: Any) -> FakeStream:
        self.created.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return FakeStream(self._next(), self.thinking, self.cite)

    def count_tokens(self, **kwargs: Any) -> SimpleNamespace:
        self.counted.append(kwargs)
        if isinstance(self.token_count, Exception):
            raise self.token_count
        return SimpleNamespace(input_tokens=self.token_count)


class FakeClient:
    """Enough of an Anthropic client for advisor.py, with nothing behind it."""

    def __init__(
        self,
        response: SimpleNamespace | Exception | list[SimpleNamespace] | None = None,
        token_count: int | Exception = 400,
    ) -> None:
        self.messages = FakeMessages(response or fake_response(), token_count)

    @property
    def sent(self) -> dict[str, Any]:
        """The keyword arguments of the last stream() call."""
        return self.messages.created[-1]


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def structured_client() -> FakeClient:
    """A client whose reply is a full recommendation, as a first turn gets."""
    parsed = Recommendation.model_validate_json(FULL_JSON)
    return FakeClient(fake_response(text=FULL_JSON, parsed=parsed))


class ApiStub:
    """Stands in for the two API entry points server.py and main.py call.

    Records every call, and returns the full recommendation for a structured
    turn and the follow-up text otherwise -- the same split the real advisor
    makes. Progress is reported the way a real streaming call reports it, so the
    endpoints' event streams are exercised.
    """

    def __init__(
        self, first: str = FULL_JSON, later: str = FOLLOW_UP, revised: str = REVISED_JSON
    ) -> None:
        self.first = first
        self.later = later
        # A revision is structured like the first reply but is not the same
        # architecture, which is the whole point: a test can tell which one a
        # deliverable was built from.
        self.revised = revised
        self.calls: list[dict[str, Any]] = []
        self.raise_with: Exception | None = None
        # Seconds to wait between progress reports. The browser suite turns this
        # up so a half-written reply is on screen long enough to look at.
        self.delay = 0.0

    def _usage(self, output_tokens: int) -> dict[str, Any]:
        return advisor.usage_of(fake_response(output_tokens=output_tokens))

    def _reply(self, text: str, structured: bool, on_progress) -> advisor.Reply:
        if on_progress:
            # Nothing but reasoning, then the reply a third at a time, which is
            # the shape a real streamed turn has.
            on_progress(advisor.Progress("", FAKE_THINKING))
            for third in (1, 2, 3):
                if self.delay:
                    time.sleep(self.delay)
                on_progress(advisor.Progress(text[: len(text) * third // 3], FAKE_THINKING))
        if self.delay:
            time.sleep(self.delay)
        parsed = Recommendation.model_validate_json(text) if structured else None
        return advisor.Reply(text, self._usage(500 if structured else 200), parsed)

    def call_claude(self, client, messages, kind="follow_up", on_progress=None, constraints=""):
        self.calls.append(
            {
                "kind": kind,
                "messages": list(messages),
                "call": "call_claude",
                # F5. Recorded rather than applied: what with_constraints does
                # with it is advisor.py's business and is tested there. What a
                # test here wants to know is whether the endpoint passed it on.
                "constraints": constraints,
            }
        )
        if self.raise_with:
            raise self.raise_with
        structured = kind in advisor.STRUCTURED_KINDS
        if kind == "revision":
            text = self.revised
        elif structured:
            text = self.first
        else:
            text = self.later
        return self._reply(text, structured, on_progress)

    def advise(self, client, workload, kind="recommendation", on_progress=None, constraints=""):
        self.calls.append(
            {
                "kind": kind,
                "workload": workload,
                "call": "advise",
                "constraints": constraints,
            }
        )
        if self.raise_with:
            raise self.raise_with
        return self._reply(self.first, True, on_progress)

    @property
    def count(self) -> int:
        return len(self.calls)


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> ApiStub:
    """Patch the API out of server.py and hand back the recorder."""
    stub = ApiStub()
    monkeypatch.setattr(server, "call_claude", stub.call_claude)
    monkeypatch.setattr(server, "advise", stub.advise)
    monkeypatch.setattr(server, "get_client", lambda: object())
    return stub


@pytest.fixture
def client(api: ApiStub, conversations):
    """A Flask test client with the API stubbed and a throwaway store.

    Rate limiting is off by default: almost every test here makes more requests
    in a second than a person would in a minute, which is the point of the
    limit. The tests that are about the limit turn it back on.
    """
    server.app.config.update(TESTING=True)
    server._payload_for.cache_clear()
    server.limiter.enabled = False
    server.limiter.reset()
    with server.app.test_client() as test_client:
        yield test_client
    server.limiter.enabled = False


@pytest.fixture
def limited(client):
    """The same test client, with the rate limiter switched on."""
    server.limiter.reset()
    server.limiter.enabled = True
    return client


def sse_events(response) -> list[dict[str, Any]]:
    """Read a streamed response back into the events it carried."""
    body = response.get_data(as_text=True)
    return [
        json.loads(line[len("data:") :])
        for frame in body.split("\n\n")
        for line in frame.split("\n")
        if line.startswith("data:")
    ]


def final_event(response) -> dict[str, Any]:
    """The last event of a stream: the finished reply, or the failure."""
    events = sse_events(response)
    assert events, "the stream carried no events at all"
    return events[-1]


@pytest.fixture(autouse=True)
def without_a_rasteriser(request, monkeypatch):
    """The default run behaves as though this machine had no browser (F7).

    Writing a bundle rasterises the diagram, which shells out to Chrome, and a
    test that drives a real browser is `slow` in this project. Left alone, an
    ordinary `pytest` would grow a browser dependency and minutes of runtime for
    a picture none of these tests look at.

    Pretending there is no browser also exercises the path that matters most: the
    Markdown has to link a file the bundle actually holds, whichever one that
    turns out to be. The tests marked slow are left alone, because a real browser
    is the whole point of them.
    """
    if "slow" in request.keywords:
        yield
        return
    monkeypatch.setattr(export, "chrome", lambda: None)
    export.diagram_png.cache_clear()
    yield
    export.diagram_png.cache_clear()
