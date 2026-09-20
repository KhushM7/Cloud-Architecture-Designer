"""store.py: conversations, messages, the spend ledger, and JSON in and out."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import pytest

import advisor
import store
from advisor import AdvisorError, merge_usage, usage_of
from tests.conftest import fake_response
from tests.samples import FULL_JSON

pytestmark = pytest.mark.usefixtures("conversations")

A_CONVERSATION = [
    {"role": "user", "content": "A patient records system for an NHS trust with 500 users"},
    {"role": "assistant", "content": FULL_JSON},
]


def one_usage(**over):
    return merge_usage({**usage_of(fake_response()), **over})


def on_day(day, kind="follow_up", **over):
    """Put one call in the ledger on a day of the caller's choosing.

    record_call always stamps today, which is right for it and no use to a test
    about months, so this writes the row itself.
    """
    total = one_usage(**over)
    with store.connect(write=True) as connection:
        connection.execute(
            "INSERT INTO usage (at, day, model, kind, calls, input_tokens, output_tokens,"
            " cache_read_tokens, cache_write_tokens, searches, cost_usd)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"{day}T12:00:00+01:00",
                day,
                advisor.MODEL,
                kind,
                *(int(total[field]) for field in advisor.COUNT_FIELDS),
                float(total["costUsd"]),
            ),
        )


# --------------------------------------------------------------------------- #
# Titles (A5)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("A shop", "A shop"),
        ("   spaced   out\n words ", "spaced out words"),
        ("", ""),
        # Cut on a word boundary, with the ellipsis marking that it was cut.
        ("A patient records system for an NHS trust", "A patient records system for an…"),
        # No boundary worth cutting on: one long word is cut where it falls.
        ("Supercalifragilisticexpialidociousandthensome", "Supercalifragilisticexpialidocious…"),
    ],
)
def test_a_conversation_is_named_after_the_question_that_started_it(text, expected):
    assert store.title_from(text) == expected


def test_a_title_is_trimmed_and_capped():
    assert store.clean_title("  Retail  ") == "Retail"
    assert store.clean_title("   ") is None
    assert store.clean_title(None) is None
    assert len(store.clean_title("T" * 500)) == store.MAX_TITLE


# --------------------------------------------------------------------------- #
# Saving and reading
# --------------------------------------------------------------------------- #


def test_a_saved_conversation_carries_its_context():
    conversation_id = store.save(A_CONVERSATION, mode="advise", title="Retail", usage=one_usage())
    saved = store.read(conversation_id)

    assert saved["title"] == "Retail"
    assert saved["titled"] is True
    assert saved["mode"] == "advise"
    assert saved["model"] == advisor.MODEL
    assert saved["version"] == store.SAVE_VERSION
    assert saved["savedAt"]
    assert saved["usage"]["calls"] == 1
    assert [message["role"] for message in saved["messages"]] == ["user", "assistant"]
    assert saved["messages"][1]["content"] == FULL_JSON


def test_an_unnamed_conversation_is_named_after_its_first_question():
    conversation_id = store.save(A_CONVERSATION)
    saved = store.read(conversation_id)

    assert saved["titled"] is False
    assert saved["title"] == "A patient records system for an…"


def test_a_conversation_with_no_calls_does_not_claim_a_cost():
    assert store.read(store.save(A_CONVERSATION))["usage"] is None
    assert store.read(store.save(A_CONVERSATION, usage=merge_usage()))["usage"] is None


def test_saving_nothing_is_refused():
    with pytest.raises(AdvisorError) as caught:
        store.save([])
    assert caught.value.kind == "empty"


def test_reading_something_that_is_not_there():
    assert store.read(999) is None
    assert store.export_json(999) is None


def test_messages_keep_the_order_they_were_asked_in():
    turns = [
        {"role": "user", "content": f"question {n}"}
        if n % 2 == 0
        else {"role": "assistant", "content": f"answer {n}"}
        for n in range(10)
    ]
    saved = store.read(store.save(turns))
    assert [message["content"] for message in saved["messages"]] == [
        turn["content"] for turn in turns
    ]


def test_the_unicode_in_a_reply_survives_the_round_trip():
    """The £ signs and emoji in a reply have to come back as they went in."""
    text = "✅ £400–700/month"
    saved = store.read(store.save([{"role": "assistant", "content": text}]))
    assert saved["messages"][0]["content"] == text


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


def test_an_empty_store_lists_nothing():
    assert store.listing() == []


def test_the_newest_conversation_comes_first():
    ids = [store.save(A_CONVERSATION, title=f"C{n}") for n in range(5)]
    listed = store.listing()

    assert [item["id"] for item in listed] == list(reversed(ids))
    assert [item["title"] for item in listed] == [f"C{n}" for n in reversed(range(5))]


def test_saves_inside_one_second_do_not_collide():
    """A1: the old store used the filename as a primary key, and paid for it."""
    ids = [store.save(A_CONVERSATION, title=f"C{n}") for n in range(20)]
    assert len(set(ids)) == 20
    assert len(store.listing()) == 20


def test_saves_racing_each_other_all_survive():
    def save(n):
        return store.save(A_CONVERSATION, title=f"R{n}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(save, range(16)))

    assert len(set(ids)) == 16
    titles = sorted(item["title"] for item in store.listing())
    assert titles == sorted(f"R{n}" for n in range(16))


def test_a_listed_conversation_describes_itself():
    store.save(A_CONVERSATION, mode="compare")
    item = store.listing()[0]

    assert item["titled"] is False
    assert item["title"] == "A patient records system for an…"
    assert item["messageCount"] == 2
    assert item["mode"] == "compare"
    assert item["model"] == advisor.MODEL
    assert item["savedAt"]


# --------------------------------------------------------------------------- #
# Renaming and deleting
# --------------------------------------------------------------------------- #


def test_renaming_changes_the_title_and_nothing_else():
    conversation_id = store.save(A_CONVERSATION)
    assert store.rename(conversation_id, "  Renamed  ") == "Renamed"

    saved = store.read(conversation_id)
    assert saved["title"] == "Renamed"
    assert saved["titled"] is True
    assert len(saved["messages"]) == 2


def test_a_blank_rename_is_refused():
    conversation_id = store.save(A_CONVERSATION)
    with pytest.raises(AdvisorError) as caught:
        store.rename(conversation_id, "   ")
    assert caught.value.kind == "empty"


def test_renaming_something_that_is_not_there():
    assert store.rename(999, "x") is None


def test_deleting_takes_the_messages_with_it():
    conversation_id = store.save(A_CONVERSATION)
    assert store.delete(conversation_id) is True
    assert store.read(conversation_id) is None
    assert store.delete(conversation_id) is False

    with store.connect() as connection:
        left = connection.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (conversation_id,)
        ).fetchone()[0]
    assert left == 0


# --------------------------------------------------------------------------- #
# JSON, in and out
# --------------------------------------------------------------------------- #


def test_an_exported_conversation_is_the_document_the_old_store_wrote():
    conversation_id = store.save(A_CONVERSATION, mode="advise", title="Retail", usage=one_usage())
    payload = store.export_json(conversation_id)

    assert set(payload) == {"timestamp", "version", "model", "mode", "messages", "title", "usage"}
    assert payload["mode"] == "advise"
    assert payload["version"] == store.SAVE_VERSION
    assert payload["messages"] == A_CONVERSATION
    assert payload["usage"]["calls"] == 1
    # And it is JSON, not something that merely looks like it.
    assert json.loads(json.dumps(payload)) == payload


def test_the_optional_fields_are_left_out_when_empty():
    payload = store.export_json(store.save(A_CONVERSATION)) or {}
    assert "title" not in payload
    assert "usage" not in payload


def test_a_conversation_survives_a_round_trip_through_json():
    original = store.save(A_CONVERSATION, mode="compare", title="Both", usage=one_usage())
    payload = store.export_json(original)

    copied = store.import_json(payload)
    assert store.export_json(copied) == payload


def test_importing_something_with_no_messages_is_refused():
    with pytest.raises(AdvisorError):
        store.import_json({"messages": []})


def test_exporting_the_lot_writes_one_file_each(tmp_path):
    store.save(A_CONVERSATION, title="One")
    store.save(A_CONVERSATION, title="Two")

    written = store.export_all(tmp_path / "out")
    assert len(written) == 2
    assert all(path.name.startswith("conversation_") for path in written)

    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert payload["messages"] == A_CONVERSATION


# --------------------------------------------------------------------------- #
# The spend ledger
# --------------------------------------------------------------------------- #


def test_nothing_spent_is_a_zero_day():
    spent = store.spent_on()
    assert spent["calls"] == 0
    assert spent["tokens"] == 0
    assert spent["costUsd"] == 0.0


def test_every_call_lands_in_the_ledger():
    store.record_call(one_usage(), "recommendation")
    store.record_call(one_usage(), "follow_up")

    spent = store.spent_on()
    assert spent["calls"] == 2
    assert spent["inputTokens"] == 2000
    assert spent["outputTokens"] == 1000
    # `tokens` is what the daily ceiling counts: everything but the call count.
    assert spent["tokens"] == 3000
    assert spent["costUsd"] > 0


def test_searches_are_counted_in_the_ledger_and_not_as_tokens():
    """F2: a search is billed per request, so the token ceiling must not count it."""
    store.record_call(one_usage(searches=3), "follow_up")

    spent = store.spent_on()
    assert spent["searches"] == 3
    assert spent["tokens"] == 1500


def test_a_saved_conversation_remembers_what_it_searched():
    conversation_id = store.save(A_CONVERSATION, usage=one_usage(searches=2))
    assert store.read(conversation_id)["usage"]["searches"] == 2


def test_a_database_written_before_searches_existed_gains_the_column(tmp_path, monkeypatch):
    """The column was added after the first release, and ALTER TABLE puts it on."""
    older = tmp_path / "older.db"
    monkeypatch.setattr(store, "DB_PATH", older)
    store.reset_for_tests()

    # The schema as it was: every table, minus the column this test is about.
    with sqlite3.connect(older) as connection:
        connection.executescript(
            "\n".join(
                line for line in store.SCHEMA.split("\n") if not line.strip().startswith("searches")
            )
        )
    store.reset_for_tests()

    conversation_id = store.save(A_CONVERSATION, usage=one_usage(searches=4))
    store.record_call(one_usage(searches=1), "follow_up")

    assert store.read(conversation_id)["usage"]["searches"] == 4
    assert store.spent_on()["searches"] == 1


def test_a_call_that_cost_nothing_is_not_recorded():
    store.record_call(None)
    store.record_call({"calls": 0})
    assert store.spent_on()["calls"] == 0


def test_yesterday_is_not_today():
    store.record_call(one_usage(), "recommendation")
    assert store.spent_on()["calls"] == 1
    assert store.spent_on(date.today() - timedelta(days=1))["calls"] == 0


def test_the_ledger_survives_a_conversation_being_deleted():
    """A call costs money whether or not the conversation is kept."""
    conversation_id = store.save(A_CONVERSATION, usage=one_usage())
    store.record_call(one_usage(), "recommendation")
    store.delete(conversation_id)

    assert store.spent_on()["calls"] == 1


# --------------------------------------------------------------------------- #
# Reading the ledger back (F14)
# --------------------------------------------------------------------------- #


def test_the_report_groups_a_month_by_day_and_kind():
    on_day("2026-03-04", "recommendation")
    on_day("2026-03-04", "follow_up")
    on_day("2026-03-11", "follow_up")

    report = store.spend_report("2026-03")

    assert report["month"] == "2026-03"
    # One row per day and kind, in date order: the query is the per-day rollup.
    assert [(row["day"], row["kind"], row["calls"]) for row in report["days"]] == [
        ("2026-03-04", "follow_up", 1),
        ("2026-03-04", "recommendation", 1),
        ("2026-03-11", "follow_up", 1),
    ]
    # And the kinds are folded out of those rows rather than asked for again.
    assert [(row["kind"], row["calls"]) for row in report["kinds"]] == [
        ("follow_up", 2),
        ("recommendation", 1),
    ]


def test_the_report_totals_agree_with_the_kinds():
    """A total that could disagree with its own breakdown would not be worth showing."""
    on_day("2026-03-04", "recommendation")
    on_day("2026-03-04", "comparison")
    on_day("2026-03-05", "follow_up", searches=2)

    report = store.spend_report("2026-03")
    kinds = report["kinds"]
    totals = report["totals"]

    assert totals["calls"] == sum(row["calls"] for row in kinds) == 3
    assert totals["tokens"] == sum(row["tokens"] for row in kinds) == 4500
    assert totals["searches"] == 2
    assert totals["costUsd"] == pytest.approx(sum(row["costUsd"] for row in kinds))


def test_a_revision_is_reported_as_its_own_kind():
    """server.py writes a fourth kind, and a report that hid it would be short."""
    on_day("2026-03-04", "revision")

    assert [row["kind"] for row in store.spend_report("2026-03")["kinds"]] == ["revision"]


def test_a_call_in_another_month_is_not_in_this_one():
    on_day("2026-03-31", "recommendation")
    on_day("2026-04-01", "follow_up")

    march = store.spend_report("2026-03")
    april = store.spend_report("2026-04")

    assert [row["day"] for row in march["days"]] == ["2026-03-31"]
    assert [row["day"] for row in april["days"]] == ["2026-04-01"]


def test_the_turn_of_the_year_is_a_month_boundary_like_any_other():
    """December has to roll to January of the next year, not month thirteen."""
    on_day("2025-12-31", "recommendation")
    on_day("2026-01-01", "follow_up")

    assert [row["day"] for row in store.spend_report("2025-12")["days"]] == ["2025-12-31"]
    assert [row["day"] for row in store.spend_report("2026-01")["days"]] == ["2026-01-01"]


def test_an_empty_month_reports_zeroes():
    report = store.spend_report("2026-03")

    assert report["days"] == []
    assert report["kinds"] == []
    assert report["totals"]["calls"] == 0
    assert report["totals"]["tokens"] == 0
    # Zero rather than None: SUM over no rows is null, and COALESCE is why.
    assert report["totals"]["costUsd"] == 0.0


def test_a_search_is_not_counted_as_a_token_in_the_report():
    """F2, again: `tokens` is re-derived here, so it is worth pinning here too."""
    on_day("2026-03-04", "follow_up", searches=3)

    report = store.spend_report("2026-03")
    assert report["totals"]["searches"] == 3
    assert report["totals"]["tokens"] == 1500


def test_every_row_of_the_report_says_it_is_priced():
    """The front end reads the flag off each row, and a row without one reads as unpriced.

    `usage` has no `priced` column, so there is nothing to derive: a call with no
    rate for its model was still made, and still recorded.
    """
    on_day("2026-03-04", "recommendation")

    report = store.spend_report("2026-03")
    for row in (*report["days"], *report["kinds"], report["totals"], report["today"]):
        assert row["priced"] is True


def test_the_report_defaults_to_this_month():
    store.record_call(one_usage(), "recommendation")

    report = store.spend_report()
    assert report["totals"]["calls"] == 1
    # Today's figure comes from where the ceiling reads it, not out of `days`.
    assert report["today"]["calls"] == 1
    assert report["today"]["day"] == store.spent_on()["day"]


def test_a_month_the_report_cannot_read_is_refused():
    for asked in ("2026", "2026-13", "2026-00", "March", "2026-3", "'; DROP TABLE usage; --"):
        with pytest.raises(ValueError):
            store.spend_report(asked)


def test_the_ceiling_comes_from_the_environment(monkeypatch):
    monkeypatch.delenv("ADVISOR_DAILY_TOKENS", raising=False)
    assert store.daily_ceiling() == 500_000

    monkeypatch.setenv("ADVISOR_DAILY_TOKENS", "1234")
    assert store.daily_ceiling() == 1234

    # Zero is off rather than a ceiling of nothing, which is what server.py reads.
    monkeypatch.setenv("ADVISOR_DAILY_TOKENS", "0")
    assert store.daily_ceiling() == 0


# --------------------------------------------------------------------------- #
# The report on the command line (F14)
# --------------------------------------------------------------------------- #


def test_the_report_command_prints_the_month_the_kinds_and_a_total(capsys):
    store.record_call(one_usage(), "recommendation")
    store.record_call(one_usage(), "follow_up")

    assert store.main(["--report"]) == 0

    printed = capsys.readouterr().out
    assert store.spend_report()["month"] in printed
    assert "recommendation" in printed
    assert "follow_up" in printed
    assert "Total" in printed
    # No exchange rate is invented anywhere, so the currency is named.
    assert "US dollars" in printed


def test_the_report_command_reports_an_older_month(capsys):
    on_day("2026-03-04", "recommendation")

    assert store.main(["--report", "--month", "2026-03"]) == 0

    printed = capsys.readouterr().out
    assert "2026-03" in printed
    assert "2026-03-04" in printed


def test_the_report_command_names_the_ceiling(capsys, monkeypatch):
    """The ceiling used to announce itself only by refusing a request (S4)."""
    monkeypatch.setenv("ADVISOR_DAILY_TOKENS", "9000")
    store.record_call(one_usage(), "recommendation")

    assert store.main(["--report"]) == 0

    printed = capsys.readouterr().out
    assert "9,000 tokens" in printed
    assert "1,500 tokens" in printed


def test_the_report_command_says_when_there_is_no_ceiling(capsys, monkeypatch):
    monkeypatch.setenv("ADVISOR_DAILY_TOKENS", "0")

    assert store.main(["--report"]) == 0
    assert "No ceiling is set" in capsys.readouterr().out


def test_the_report_command_refuses_a_month_it_cannot_read(capsys):
    assert store.main(["--report", "--month", "March"]) == 1
    assert "YYYY-MM" in capsys.readouterr().out


def test_the_command_with_nothing_to_do_says_what_it_can_do(capsys):
    assert store.main([]) == 0
    assert "--report" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# The database itself
# --------------------------------------------------------------------------- #


def test_the_database_is_created_on_demand(tmp_path, monkeypatch):
    fresh = tmp_path / "does" / "not" / "exist" / "advisor.db"
    monkeypatch.setattr(store, "DB_PATH", fresh)
    store.reset_for_tests()

    store.save(A_CONVERSATION)
    assert fresh.is_file()


def test_reads_and_writes_can_overlap(conversations):
    """WAL, so a listing during a save does not come back locked."""

    def work(n):
        store.save(A_CONVERSATION, title=f"W{n}")
        return len(store.listing())

    with ThreadPoolExecutor(max_workers=8) as pool:
        counts = list(pool.map(work, range(16)))

    assert max(counts) == 16


def test_the_schema_is_the_tables_it_claims_to_be():
    with store.connect() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert {
        "conversations",
        "messages",
        "usage",
        "prices",
        "conversation_services",
        "tags",
    } <= tables


def test_a_message_cannot_belong_to_no_conversation():
    with pytest.raises(sqlite3.IntegrityError), store.connect(write=True) as connection:
        connection.execute(
            "INSERT INTO messages (conversation_id, position, role, content) VALUES (?, ?, ?, ?)",
            (999, 0, "user", "orphan"),
        )


# --------------------------------------------------------------------------- #
# Finding a conversation again (F8), and what it was built for (F5)
# --------------------------------------------------------------------------- #


def test_saving_files_a_conversation_under_what_it_recommended():
    conversation_id = store.save(A_CONVERSATION, compliance="nhs-dspt")
    item = next(item for item in store.listing() if item["id"] == conversation_id)

    assert item["region"] == "eu-west-2"
    assert item["tier"] == "Medium"
    assert item["compliance"] == "nhs-dspt"


def test_the_facets_come_off_the_architecture_as_it_now_stands():
    """A revision supersedes what it revised, so the last one wins."""
    revised = json.loads(FULL_JSON)
    revised["region"] = "eu-west-1"
    revised["cost"]["tier"] = "High"
    conversation_id = store.save(
        [
            *A_CONVERSATION,
            {"role": "user", "content": "Move it to Ireland"},
            {"role": "assistant", "content": json.dumps(revised)},
        ]
    )

    item = next(item for item in store.listing() if item["id"] == conversation_id)
    assert item["region"] == "eu-west-1"
    assert item["tier"] == "High"


def test_a_conversation_with_no_architecture_in_it_files_under_nothing():
    conversation_id = store.save(
        [
            {"role": "user", "content": "What is an ALB?"},
            {"role": "assistant", "content": "A load balancer."},
        ]
    )
    item = next(item for item in store.listing() if item["id"] == conversation_id)

    assert item["region"] == ""
    assert item["tier"] == ""


def test_no_compliance_profile_is_stored_as_nothing_not_as_none():
    """The column answers "what did they pick", and NULL is "they did not"."""
    conversation_id = store.save(A_CONVERSATION, compliance="none")
    assert store.read(conversation_id)["compliance"] == ""

    made_up = store.save(A_CONVERSATION, compliance="iso-27001")
    assert store.read(made_up)["compliance"] == ""


def test_searching_by_text_looks_inside_the_messages():
    store.save(A_CONVERSATION)
    store.save(
        [
            {"role": "user", "content": "A shop front for selling bicycles"},
            {"role": "assistant", "content": "Some prose."},
        ]
    )

    found = store.search("bicycles")
    assert len(found) == 1
    assert "bicycles" in found[0]["snippet"]

    assert len(store.search("NHS trust")) == 1
    assert store.search("submarines") == []


def test_searching_by_facet_needs_no_text():
    here = store.save(A_CONVERSATION)
    store.save(
        [
            {"role": "user", "content": "Something else"},
            {"role": "assistant", "content": "Prose, so no facets."},
        ]
    )

    assert [item["id"] for item in store.search(tier="Medium")] == [here]
    assert [item["id"] for item in store.search(region="eu-west-2")] == [here]
    assert [item["id"] for item in store.search(service="RDS")] == [here]
    assert store.search(tier="High") == []


def test_the_filters_narrow_together_rather_than_widen():
    store.save(A_CONVERSATION)
    assert store.search(text="NHS", tier="Medium") != []
    assert store.search(text="NHS", tier="High") == []


def test_an_empty_search_is_the_whole_list():
    store.save(A_CONVERSATION)
    assert [item["id"] for item in store.search()] == [item["id"] for item in store.listing()]


def test_a_wildcard_in_a_search_is_looked_for_rather_than_matched():
    """The percent sign is LIKE syntax, and a search box is not a query language."""
    store.save(A_CONVERSATION)
    assert store.search("%") == []


def test_tagging_is_additive_and_deduplicated():
    conversation_id = store.save(A_CONVERSATION)

    assert store.add_tag(conversation_id, "NHS") == ["nhs"]
    assert store.add_tag(conversation_id, " nhs ") == ["nhs"]
    assert store.add_tag(conversation_id, "public sector") == ["nhs", "public sector"]
    assert store.all_tags() == ["nhs", "public sector"]

    assert store.remove_tag(conversation_id, "nhs") == ["public sector"]
    assert [item["id"] for item in store.search(tag="public sector")] == [conversation_id]


def test_tagging_something_that_is_not_there_says_so_rather_than_inventing_it():
    assert store.add_tag(999, "nhs") is None
    assert store.remove_tag(999, "nhs") is None


def test_an_empty_tag_is_refused():
    conversation_id = store.save(A_CONVERSATION)
    with pytest.raises(AdvisorError):
        store.add_tag(conversation_id, "   ")


def test_deleting_a_conversation_takes_its_tags_and_facets_with_it():
    conversation_id = store.save(A_CONVERSATION)
    store.add_tag(conversation_id, "nhs")
    store.delete(conversation_id)

    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM conversation_services").fetchone()[0] == 0
    assert store.all_tags() == []


def test_a_conversation_brought_in_as_json_is_findable_too():
    """import_json does not go through save, so it derives facets of its own."""
    original = store.save(A_CONVERSATION, compliance="pci-dss")
    store.add_tag(original, "cards")
    payload = store.export_json(original)
    store.delete(original)

    imported = store.import_json(payload)
    item = next(item for item in store.listing() if item["id"] == imported)

    assert item["region"] == "eu-west-2"
    assert item["tier"] == "Medium"
    assert item["compliance"] == "pci-dss"
    assert item["tags"] == ["cards"]
    assert [found["id"] for found in store.search(service="ElastiCache")] == [imported]


def test_a_conversation_from_before_the_facets_gains_them_on_reindex():
    conversation_id = store.save(A_CONVERSATION)
    # As it would have been stored before the columns existed.
    with store.connect(write=True) as connection:
        connection.execute(
            "UPDATE conversations SET region = NULL, tier = NULL WHERE id = ?", (conversation_id,)
        )
        connection.execute(
            "DELETE FROM conversation_services WHERE conversation_id = ?", (conversation_id,)
        )
    assert store.search(tier="Medium") == []

    assert store.reindex() == 1
    assert [item["id"] for item in store.search(tier="Medium")] == [conversation_id]
    assert [item["id"] for item in store.search(service="RDS")] == [conversation_id]


def test_reindexing_leaves_the_compliance_profile_alone():
    """It was never in the reply, so there is nothing to re-derive it from.

    Q3. reindex() shares _write_facets with save() and import_json() now rather
    than repeating its three statements, and `keep_compliance=True` is what keeps
    this true across the sharing: re-deriving this one would blank a profile
    nothing can recover.
    """
    conversation_id = store.save(A_CONVERSATION, compliance="hipaa")
    store.reindex()
    assert store.read(conversation_id)["compliance"] == "hipaa"

    # The column itself, which is what a reopened conversation reads to put the
    # selector back where it was left.
    with store.connect() as connection:
        row = connection.execute(
            "SELECT compliance FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
    assert row["compliance"] == "hipaa"


@pytest.mark.parametrize("column", ["region", "tier", "compliance"])
def test_a_database_written_before_the_facet_columns_gains_them(column):
    """ADDED_COLUMNS is the only route onto a database that already exists.

    Built by taking the column back off rather than by filtering it out of
    SCHEMA, which is how the `searches` test above does it: that trick cannot
    remove the last column of a table without leaving a dangling comma, and it
    would silently strip the identically-named column from `prices` too.
    """
    assert column in store.ADDED_COLUMNS["conversations"]

    with store.connect(write=True) as connection:
        connection.execute(f"ALTER TABLE conversations DROP COLUMN {column}")
        present = {
            row["name"] for row in connection.execute("PRAGMA table_info(conversations)").fetchall()
        }
        assert column not in present

    store.reset_for_tests()

    conversation_id = store.save(A_CONVERSATION, compliance="hipaa")
    saved = store.read(conversation_id)
    assert saved["region"] == "eu-west-2"
    assert saved["tier"] == "Medium"
    assert saved["compliance"] == "hipaa"


# --------------------------------------------------------------------------- #
# Search (F8, P2)
# --------------------------------------------------------------------------- #


def test_a_text_search_still_says_why_it_matched():
    """P2. The snippet comes off the row now rather than from a query per result.

    It used to be one extra SELECT for every conversation found, up to
    MAX_SEARCH_RESULTS of them on each debounced keystroke, for a string the row
    was already being read to produce. The behaviour has to be identical.
    """
    store.save(A_CONVERSATION)

    found = store.search(text="patient records")

    assert len(found) == 1
    assert "patient records" in found[0]["snippet"]


def test_a_search_that_matches_only_a_title_has_no_snippet_to_show():
    conversation_id = store.save(A_CONVERSATION)
    store.rename(conversation_id, "Northbridge onboarding")

    found = store.search(text="Northbridge")

    assert len(found) == 1
    # The title matched, no message did, so there is nothing to quote.
    assert found[0]["snippet"] == ""


def test_the_snippet_is_taken_from_the_first_message_that_matched():
    store.save(
        [
            {"role": "user", "content": "Something else entirely"},
            {"role": "assistant", "content": "A reply mentioning Aurora once"},
            {"role": "user", "content": "And Aurora again later"},
        ]
    )

    found = store.search(text="Aurora")

    assert found[0]["snippet"].startswith("A reply mentioning Aurora")


def test_a_search_box_is_not_a_query_language():
    """The needle now binds ahead of every filter, so the order cannot drift."""
    store.save(
        [
            {"role": "user", "content": "Costs 100% of the budget"},
            {"role": "assistant", "content": FULL_JSON},
        ]
    )
    store.save(A_CONVERSATION)

    assert len(store.search(text="100%")) == 1
    # A bare `%` is one character to look for, not "anything at all": it finds
    # the conversation that has one in it and not the one that does not.
    assert len(store.search(text="%")) == 1
    assert len(store.search(text="100% of the budget")) == 1
    # `_` is LIKE's single-character wildcard, so "1_0" would match "100" if it
    # were being passed through. It is not there literally, so nothing matches.
    assert store.search(text="1_0") == []


def test_a_text_search_combined_with_a_facet_filters_on_both():
    """The parameters are bound in two places now, so this is worth pinning."""
    store.save(A_CONVERSATION, compliance="nhs-dspt")

    assert len(store.search(text="patient", region="eu-west-2")) == 1
    assert store.search(text="patient", region="us-east-1") == []
    assert store.search(text="nothing here", region="eu-west-2") == []
