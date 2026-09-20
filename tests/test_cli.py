"""main.py: argument parsing, the two run modes, and what they print."""

import io
import sqlite3
import sys

import pytest
from rich.cells import cell_len

import advisor
import main
import pricing
import store
from advisor import AdvisorError
from tests.conftest import ApiStub, fake_response


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch) -> ApiStub:
    """Patch the API out of main.py and hand back the recorder."""
    stub = ApiStub()
    monkeypatch.setattr(main, "call_claude", stub.call_claude)
    monkeypatch.setattr(main, "advise", stub.advise)
    monkeypatch.setattr(main, "get_client", lambda: object())
    return stub


def run(argv, stdin="") -> tuple[object, str]:
    """Run the CLI with a scripted stdin, returning (exit code, output)."""
    captured = io.StringIO()
    code: object
    real_stdin, real_stdout = sys.stdin, sys.stdout
    try:
        sys.stdin = io.StringIO(stdin)
        sys.stdout = captured
        code = main.main(argv)
    except SystemExit as stop:  # prompt_for() exits on an empty description
        code = 0 if stop.code is None else stop.code
    finally:
        sys.stdin, sys.stdout = real_stdin, real_stdout
    return code, captured.getvalue()


# --------------------------------------------------------------------------- #
# Arguments
# --------------------------------------------------------------------------- #


def test_two_workloads_need_compare():
    with pytest.raises(SystemExit):
        main.parse_args(["-w", "a", "-w", "b"])


def test_compare_takes_two_workloads():
    args = main.parse_args(["--compare", "-w", "a", "-w", "b"])
    assert args.compare is True
    assert args.workload == ["a", "b"]


def test_no_arguments_prompts_instead():
    args = main.parse_args([])
    assert args.workload is None
    assert args.compare is False
    assert args.no_save is False


# --------------------------------------------------------------------------- #
# The advisory run
# --------------------------------------------------------------------------- #


def test_a_run_prints_the_recommendation_and_takes_a_follow_up(cli, conversations):
    code, output = run(["-w", "An online shop"], stdin="How do I cut the cost?\nexit\n")

    assert code == 0
    assert cli.count == 2
    assert "Recommended Services" in output
    assert "Ask a follow-up question" in output
    # The follow-up carried the whole thread, not just the new question.
    assert len(cli.calls[1]["messages"]) == 3


def test_a_run_reports_what_it_spent(cli, conversations):
    code, output = run(["-w", "A shop", "--no-save"], stdin="And on EC2?\nexit\n")
    assert code == 0
    assert "2 API calls" in output
    assert "$0.0" in output


def test_a_run_is_saved_with_its_cost(cli, conversations):
    code, output = run(["-w", "A shop"], stdin="exit\n")

    assert code == 0
    assert "Saved as conversation" in output

    saved = store.listing()
    assert len(saved) == 1
    conversation = store.read(saved[0]["id"])
    assert conversation["mode"] == "advise"
    assert len(conversation["messages"]) == 2
    assert conversation["usage"]["calls"] == 1


def test_the_cli_and_the_web_app_count_against_the_same_ledger(cli, conversations):
    """A1: one API key, one budget, one place the spend is written down."""
    run(["-w", "A shop"], stdin="What about cost?\nexit\n")
    assert store.spent_on()["calls"] == 2


def test_no_save_stores_nothing(cli, conversations):
    run(["-w", "A shop", "--no-save"], stdin="exit\n")
    assert store.listing() == []


def test_a_follow_up_that_fails_is_reported_and_not_kept(cli, conversations, monkeypatch):
    """The failed question is dropped so the thread stays usable."""
    calls = {"n": 0}
    real = cli.call_claude

    def flaky(client, messages, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise AdvisorError(
                "cut short", kind="truncated", usage=advisor.usage_of(fake_response())
            )
        return real(client, messages, *args, **kwargs)

    monkeypatch.setattr(main, "call_claude", flaky)
    code, output = run(["-w", "A shop"], stdin="A bad question\nexit\n")

    assert code == 0
    assert "cut short" in output
    # Both calls are counted, including the one that failed after being billed.
    assert "2 API calls" in output

    conversation = store.read(store.listing()[0]["id"])
    assert [message["role"] for message in conversation["messages"]] == ["user", "assistant"]


# --------------------------------------------------------------------------- #
# The comparison run
# --------------------------------------------------------------------------- #


def test_a_comparison_prints_two_columns(cli, conversations):
    code, output = run(["--compare", "-w", "Serverless", "-w", "On EC2", "--no-save"])

    assert code == 0
    assert cli.count == 2
    assert "Option A" in output and "Option B" in output
    assert "│" in output

    rows = [line for line in output.splitlines() if " │ " in line]
    assert len(rows) > 10
    # rich trims trailing spaces, so only the left column's width is checkable.
    widths = {cell_len(line.split(" │ ")[0]) for line in rows}
    assert max(widths) - min(widths) <= 2


def test_a_comparison_is_saved_as_four_messages(cli, conversations):
    run(["--compare", "-w", "A", "-w", "B"])
    payload = store.read(store.listing()[0]["id"])

    assert payload["mode"] == "compare"
    assert [message["role"] for message in payload["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert payload["usage"]["calls"] == 2


# --------------------------------------------------------------------------- #
# Prompts and failures
# --------------------------------------------------------------------------- #


def test_an_empty_description_stops_with_an_explanation(cli, conversations):
    code, output = run(["--no-save"], stdin="\n")
    assert code == 1
    assert "cannot be empty" in output


def test_ctrl_d_at_the_prompt_exits_quietly(cli, conversations):
    code, output = run(["--no-save"], stdin="")
    assert code == 0
    assert "Exiting" in output


def test_a_failure_on_the_first_call_exits_one_and_shows_the_cost(cli, conversations):
    cli.raise_with = AdvisorError(
        "ran out of room", kind="truncated", usage=advisor.usage_of(fake_response())
    )
    code, output = run(["-w", "A shop", "--no-save"])

    assert code == 1
    assert "ran out of room" in output
    assert "1 API call" in output
    assert store.listing() == []


def test_a_missing_key_is_reported_plainly(monkeypatch, conversations):
    monkeypatch.setattr(
        main, "get_client", lambda: (_ for _ in ()).throw(AdvisorError("no key", "missing_key"))
    )
    code, output = run(["-w", "A shop", "--no-save"])
    assert code == 1
    assert "no key" in output


# --------------------------------------------------------------------------- #
# Small pieces
# --------------------------------------------------------------------------- #


def test_the_usage_line_reads_like_a_sentence():
    one_call = {"calls": 1, "inputTokens": 1000, "outputTokens": 500, "costUsd": 0.0095}
    assert main.usage_line(one_call) == "1 API call · 1,000 in / 500 out tokens · $0.0095"

    many = {"calls": 12, "inputTokens": 12345, "outputTokens": 6789, "costUsd": 1.5}
    assert main.usage_line(many).startswith("12 API calls · 12,345 in / 6,789 out")


def test_an_unpriced_model_says_so():
    line = main.usage_line(
        {"calls": 1, "inputTokens": 1, "outputTokens": 1, "costUsd": 0.0, "priced": False}
    )
    assert "cost unknown" in line


def test_read_line_turns_the_end_of_input_into_a_stop(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": (_ for _ in ()).throw(EOFError))
    with pytest.raises(main.Interrupted):
        main.read_line(">> ")


def test_padding_accounts_for_wide_characters():
    """Emoji are two cells wide, so len() would misalign the columns."""
    assert cell_len(main.pad("✅ ok", 10)) == 10
    assert cell_len(main.pad("plain", 10)) == 10


# --------------------------------------------------------------------------- #
# What a run costs, counted whether or not it worked (C3, T1)
# --------------------------------------------------------------------------- #


def ledger():
    """Every call the ledger recorded, as (kind, calls) pairs."""
    with store.connect() as connection:
        rows = connection.execute("SELECT kind, calls FROM usage ORDER BY id").fetchall()
    return [(row["kind"], row["calls"]) for row in rows]


def test_a_recommendation_that_fails_is_still_counted(cli, conversations):
    """C3. The opening call is billed even when the reply is unusable.

    A reply truncated at the token cap costs what it cost, and the ledger is what
    the web app's daily ceiling counts. This path used to raise straight past the
    ledger, so the CLI could spend real money and leave no trace -- the follow-up
    loop had always recorded it, and the first call never did.
    """
    cli.raise_with = AdvisorError(
        "cut short", kind="truncated", usage=advisor.usage_of(fake_response())
    )

    code, output = run(["-w", "A shop"])

    assert code == 1
    assert "cut short" in output
    assert ledger() == [("recommendation", 1)]


def test_a_comparison_that_fails_halfway_counts_every_column(cli, conversations, monkeypatch):
    """C3. pool.map raised on the first failure and threw away the rest.

    Four architectures is four calls and four bills. The columns that succeeded
    had already been paid for by the time one of them failed, so they are
    recorded before the failure is reported.
    """
    calls = {"n": 0}
    real = cli.advise

    def flaky(client, workload, *args, **kwargs):
        calls["n"] += 1
        if workload == "On EC2":
            raise AdvisorError("declined", kind="refusal", usage=advisor.usage_of(fake_response()))
        return real(client, workload, *args, **kwargs)

    monkeypatch.setattr(main, "advise", flaky)
    code, output = run(["--compare", "-w", "Serverless", "-w", "On EC2"])

    assert code == 1
    assert "declined" in output
    # Both columns, not just the one that came back.
    assert sorted(ledger()) == [("comparison", 1), ("comparison", 1)]


def test_a_run_reaches_no_network_at_all(cli, conversations):
    """T1. The README says the default run touches nothing, and now it does not.

    pricing.estimate warms the region whose rates are not cached, and the
    throwaway store means they never are -- so eight tests in this file were each
    streaming AWS's real price list files, the 210 MB EC2 one included. The
    conftest guard blocks it at urlopen; this is the test that says the CLI is
    still fine when it is blocked, and reports an estimate from the advisor's own
    figures rather than failing.
    """
    reached = []
    real = pricing.warm

    def watched(region, *args, **kwargs):
        reached.append(region)
        return real(region, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(pricing, "warm", watched)
        code, output = run(["-w", "A shop", "--no-save"], stdin="exit\n")

    assert code == 0
    # It tried, was refused, and said what it could anyway.
    assert reached == ["eu-west-2"]
    assert "Estimated monthly cost" in output
    assert store.prices_for("eu-west-2") == {}


def test_a_ledger_that_cannot_be_written_does_not_lose_the_answer(cli, conversations, monkeypatch):
    """The rule server._record already follows, now followed here too.

    The reply is in the user's hands either way, and a row missing from the spend
    table is a smaller problem than a traceback on top of a recommendation they
    waited twenty seconds for.
    """

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(main.store, "record_call", broken)
    code, output = run(["-w", "A shop", "--no-save"], stdin="exit\n")

    assert code == 0
    assert "Recommended Services" in output
