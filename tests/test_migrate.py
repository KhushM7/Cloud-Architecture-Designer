"""migrate.py: bringing conversations saved by an earlier version into the store.

The Markdown heuristics these exercise used to live in parse.py and run on every
reply. They now run once, over old files, and nowhere else.
"""

import json

import pytest

import store
from migrate import IMPORTED_SUFFIX, UNPRICED, as_recommendation, convert, import_file, main
from schema import Recommendation
from tests.samples import FOLLOW_UP, FULL_JSON, LEGACY_REPLY, NOTE_COUNT, SERVICE_COUNT

pytestmark = pytest.mark.usefixtures("conversations")


@pytest.fixture(scope="module")
def converted():
    return as_recommendation(LEGACY_REPLY)


# --------------------------------------------------------------------------- #
# Reading one old reply
# --------------------------------------------------------------------------- #


def test_an_old_reply_becomes_a_valid_recommendation(converted):
    assert Recommendation.model_validate(converted)


def test_the_headline_survives(converted):
    assert converted["headline"] == (
        "Spread the load, cache the reads, scale back down after the sale"
    )


def test_the_services_table_is_read_in_order(converted):
    assert len(converted["services"]) == SERVICE_COUNT
    assert converted["services"][0] == {
        "name": "CloudFront",
        "purpose": "Content delivery network",
        "reasoning": "Serves images, CSS and JS from edge locations.",
        "usage": [UNPRICED],
    }
    assert converted["services"][-1]["name"] == "RDS Read Replica"


def test_nothing_migrated_pretends_to_be_priceable(converted):
    """F1: an old reply has no instance types in it, so it has no cost in it."""
    assert converted["region"] == "eu-west-2"
    assert all(service["usage"] == [UNPRICED] for service in converted["services"])


def test_the_notes_keep_their_pillar_and_their_mark(converted):
    notes = converted["notes"]
    assert len(notes) == NOTE_COUNT
    assert [note["status"] for note in notes] == ["good", "good", "good", "review", "review"]
    assert [note["pillar"] for note in notes] == [
        "Reliability",
        "Reliability",
        "Performance",
        "Security",
        "Cost",
    ]
    # The tick itself was decoration; the status field carries what it meant.
    assert notes[0]["text"].startswith("Multi-AZ RDS removes")
    assert "✅" not in notes[0]["text"]


def test_the_cost_tier_and_the_diagram_survive(converted):
    assert converted["cost"]["tier"] == "Medium"
    assert converted["cost"]["detail"].startswith("£1,500–3,000/month during peak")
    assert converted["diagram"].startswith("flowchart LR")


def test_a_follow_up_is_left_as_markdown():
    assert as_recommendation(FOLLOW_UP) is None


@pytest.mark.parametrize(
    "text",
    ["", "   ", "just some prose", "## AWS Architecture Recommendation\n"],
)
def test_nothing_recognisable_is_left_alone(text):
    assert as_recommendation(text) is None


# --------------------------------------------------------------------------- #
# The shapes the old parser had to cope with
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("bullet", "pillar", "status"),
    [
        ("- ✅ **Reliability**: fine", "Reliability", "good"),
        ("- ⚠️ **Security**: check", "Security", "review"),
        ("- ❗ Cost - too high", "Cost", "review"),
        ("- ✔ Performance: quick", "Performance", "good"),
        # A tick wins outright, so a bullet carrying both is handled well.
        ("- ✅ ⚠️ **Cost**: mixed signals", "Cost", "good"),
        # The old parser allowed any label at all. The six pillars are what the
        # schema has room for, so anything else is filed under Operations.
        ("- ✅ Backups run nightly", "Operations", "good"),
        ("- ⚠️ **Data residency**: confirm the region", "Operations", "review"),
        ("- ✅ **Operational Excellence**: runbooks exist", "Operations", "good"),
    ],
)
def test_note_shapes(bullet, pillar, status):
    converted = as_recommendation(f"### Well-Architected Notes\n{bullet}\n")
    assert converted["notes"] == [
        {"pillar": pillar, "status": status, "text": converted["notes"][0]["text"]}
    ]


@pytest.mark.parametrize(
    ("body", "tier", "detail_starts"),
    [
        ("**Medium** — £400/month", "Medium", "£400/month"),
        ("**Low–Medium** — £150-400/month", "Low–Medium", "£150-400/month"),
        ("**Medium** to **High**: varies", "Medium–High", "varies"),
        # A straddle the schema has no tier for keeps the first half it read.
        ("**High** to **Medium**: varies", "High", "varies"),
        # No tier stated at all is recorded as such rather than invented.
        ("Roughly £900 a month", "Medium", "(tier not stated) Roughly £900 a month"),
    ],
)
def test_cost_tier_shapes(body, tier, detail_starts):
    cost = as_recommendation(f"### Cost Tier\n{body}\n")["cost"]
    assert cost["tier"] == tier
    assert cost["detail"].startswith(detail_starts)


def test_service_columns_are_found_by_name_not_position():
    converted = as_recommendation(
        "### Recommended Services\n"
        "| Reasoning | Service | Purpose |\n|---|---|---|\n"
        "| Managed | ECS | Run the API |\n"
    )
    assert converted["services"] == [
        {"name": "ECS", "purpose": "Run the API", "reasoning": "Managed", "usage": [UNPRICED]}
    ]


def test_a_short_row_leaves_the_missing_cells_empty():
    converted = as_recommendation(
        "### Recommended Services\n| Service | Purpose | Reasoning |\n|---|---|---|\n| S3 |\n"
    )
    assert converted["services"] == [
        {"name": "S3", "purpose": "", "reasoning": "", "usage": [UNPRICED]}
    ]


# --------------------------------------------------------------------------- #
# Importing a file into the store
# --------------------------------------------------------------------------- #


def write_saved(directory, messages, name="conversation_20260101_120000.json", **extra):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(
        json.dumps({"timestamp": "2026-01-01T12:00:00+00:00", "messages": messages, **extra}),
        encoding="utf-8",
    )
    return path


def test_converting_a_reply_leaves_a_follow_up_alone():
    payload = {
        "messages": [
            {"role": "user", "content": "A shop"},
            {"role": "assistant", "content": LEGACY_REPLY},
            {"role": "user", "content": "Is the replica worth it?"},
            {"role": "assistant", "content": FOLLOW_UP},
        ]
    }
    assert convert(payload) == 1

    Recommendation.model_validate_json(payload["messages"][1]["content"])
    assert payload["messages"][3]["content"] == FOLLOW_UP
    assert payload["version"] == store.SAVE_VERSION


def test_a_file_is_imported_and_the_original_kept(tmp_path):
    path = write_saved(
        tmp_path,
        [
            {"role": "user", "content": "A shop"},
            {"role": "assistant", "content": LEGACY_REPLY},
        ],
        title="Black Friday",
    )

    assert "imported" in import_file(path)
    assert not path.exists()
    assert path.with_suffix(IMPORTED_SUFFIX).is_file()

    saved = store.read(store.listing()[0]["id"])
    assert saved["title"] == "Black Friday"
    assert saved["version"] == store.SAVE_VERSION
    Recommendation.model_validate_json(saved["messages"][1]["content"])


def test_a_conversation_already_in_the_new_format_is_imported_unchanged(tmp_path):
    path = write_saved(
        tmp_path,
        [{"role": "assistant", "content": FULL_JSON}],
        version=store.SAVE_VERSION,
    )
    assert "0 replies converted" not in import_file(path)

    saved = store.read(store.listing()[0]["id"])
    assert saved["messages"][0]["content"] == FULL_JSON


def test_running_it_twice_imports_nothing_twice(tmp_path):
    write_saved(tmp_path, [{"role": "assistant", "content": LEGACY_REPLY}])

    assert main(["--dir", str(tmp_path)]) == 0
    assert len(store.listing()) == 1

    assert main(["--dir", str(tmp_path)]) == 0
    assert len(store.listing()) == 1


def test_a_dry_run_changes_nothing(tmp_path, capsys):
    path = write_saved(tmp_path, [{"role": "assistant", "content": LEGACY_REPLY}])
    before = path.read_text(encoding="utf-8")

    assert main(["--dir", str(tmp_path), "--dry-run"]) == 0
    assert "would import" in capsys.readouterr().out
    assert path.read_text(encoding="utf-8") == before
    assert store.listing() == []


def test_a_file_that_is_not_a_conversation_is_skipped(tmp_path):
    path = tmp_path / "conversation_20260101_120000.json"
    tmp_path.mkdir(parents=True, exist_ok=True)
    path.write_text('{"not": "a conversation"}', encoding="utf-8")
    assert "skipped" in import_file(path)

    path.write_text("{ broken", encoding="utf-8")
    assert "skipped" in import_file(path)
    assert store.listing() == []


def test_one_bad_file_does_not_stop_the_rest(tmp_path, capsys):
    write_saved(tmp_path, [{"role": "assistant", "content": LEGACY_REPLY}])
    (tmp_path / "conversation_20250101_000000.json").write_text("{ broken", encoding="utf-8")

    assert main(["--dir", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "skipped" in output and "imported" in output
    assert len(store.listing()) == 1


def test_a_missing_directory_is_an_error(tmp_path, capsys):
    assert main(["--dir", str(tmp_path / "nope")]) == 1
    assert "No such directory" in capsys.readouterr().out


def test_an_empty_directory_is_not(tmp_path, capsys):
    tmp_path.mkdir(parents=True, exist_ok=True)
    assert main(["--dir", str(tmp_path)]) == 0
    assert "Nothing to import" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Back out again
# --------------------------------------------------------------------------- #


def test_everything_can_be_exported_back_to_json(tmp_path, capsys):
    store.save([{"role": "user", "content": "A shop"}], title="One")
    store.save([{"role": "user", "content": "A pipeline"}], title="Two")

    out = tmp_path / "out"
    assert main(["--export", str(out)]) == 0
    assert "Wrote 2 conversations" in capsys.readouterr().out

    files = sorted(out.glob("conversation_*.json"))
    assert len(files) == 2
    assert {json.loads(path.read_text(encoding="utf-8"))["title"] for path in files} == {
        "One",
        "Two",
    }


def test_an_export_can_be_imported_again(tmp_path):
    original = store.save(
        [{"role": "user", "content": "A shop"}, {"role": "assistant", "content": FULL_JSON}],
        title="Round trip",
    )
    out = tmp_path / "out"
    main(["--export", str(out)])
    store.delete(original)

    assert main(["--dir", str(out)]) == 0
    restored = store.read(store.listing()[0]["id"])
    assert restored["title"] == "Round trip"
    assert restored["messages"][1]["content"] == FULL_JSON


def test_reindexing_makes_an_older_conversation_findable_again(capsys):
    """F8: a conversation saved before the facet columns had nothing to file it under."""
    conversation_id = store.save(
        [
            {"role": "user", "content": "A patient records system"},
            {"role": "assistant", "content": FULL_JSON},
        ]
    )
    with store.connect(write=True) as connection:
        connection.execute("UPDATE conversations SET region = NULL, tier = NULL")
        connection.execute("DELETE FROM conversation_services")

    assert store.search(tier="Medium") == []
    assert main(["--reindex"]) == 0
    assert "Reindexed 1 conversation." in capsys.readouterr().out
    assert [item["id"] for item in store.search(tier="Medium")] == [conversation_id]
