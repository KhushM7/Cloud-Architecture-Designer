"""pricing.py: reading the AWS Price List, and what an architecture costs.

Nothing here reaches AWS except the two tests marked `network`, which are opt-in
and exist to notice the price list changing shape under us. Everything else
feeds the same code rows it would have read from a file.
"""

import io
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

import pricing
import store
from schema import Meter, Recommendation
from tests.samples import FULL_JSON, PRICED_SERVICE_COUNT, PRICES, SERVICE_COUNT

pytestmark = pytest.mark.usefixtures("conversations")

# A price list row has some ninety columns; these are the ones any of the
# filters look at, which is what a fake row has to carry.
COLUMNS = [
    "TermType",
    "PricePerUnit",
    "Unit",
    "StartingRange",
    "Product",
    "Family",
    "usageType",
    "Instance",
    "Type",
    "Operating",
    "System",
    "Tenancy",
    "Pre",
    "Installed",
    "S/W",
    "CapacityStatus",
    "Volume",
    "API",
    "Name",
    "Database",
    "Engine",
    "Deployment",
    "Option",
    "Cache",
    "Engine",
    "Volume",
    "Type",
    "Group",
]


def row(**over):
    """One price list row: on-demand, priced, and otherwise blank."""
    base = dict.fromkeys(COLUMNS, "")
    base.update({"TermType": "OnDemand", "PricePerUnit": "1.0", "StartingRange": "0"})
    # Two of the column names have spaces in them, so they cannot be keywords.
    base.update({key.replace("_", " "): value for key, value in over.items()})
    return base


def ec2_row(instance_type="m6i.large", price="0.111", **over):
    """A plain Linux on-demand instance row, before any override."""
    plain = {
        "Product Family": "Compute Instance",
        "Instance Type": instance_type,
        "PricePerUnit": price,
        "Unit": "Hrs",
        "Operating System": "Linux",
        "Pre Installed S/W": "NA",
        "Tenancy": "Shared",
        "CapacityStatus": "Used",
    }
    plain.update({key.replace("_", " "): value for key, value in over.items()})
    return row(**{key.replace(" ", "_"): value for key, value in plain.items()})


@pytest.fixture
def offline(monkeypatch):
    """Serve the price list from a list of rows instead of from AWS."""
    served: dict[str, list[dict]] = {}

    def rows(offer_code, region):
        if offer_code not in served:
            raise pricing.PricingError(f"nothing stubbed for {offer_code}")
        return iter(served[offer_code])

    monkeypatch.setattr(pricing, "_rows", rows)
    return served


# --------------------------------------------------------------------------- #
# Picking the right rows out of the price list
# --------------------------------------------------------------------------- #


def test_an_instance_type_is_priced_at_its_plain_rate(offline):
    offline["AmazonEC2"] = [
        ec2_row(),
        # The same instance, priced another eleven ways.
        ec2_row(price="0.222", **{"Operating System": "Windows"}),
        ec2_row(price="0.333", Tenancy="Dedicated"),
        ec2_row(price="0.444", **{"Pre Installed S/W": "SQL Std"}),
        ec2_row(price="0.555", CapacityStatus="UnusedCapacityReservation"),
        ec2_row(price="0.666", TermType="Reserved"),
    ]
    prices = pricing.extract("AmazonEC2", "eu-west-2")
    assert prices[(Meter.EC2_INSTANCE, "m6i.large")] == 0.111


def test_a_multi_az_database_is_not_priced_as_a_single_one(offline):
    """The standby is charged for, and AWS prices it as its own rate."""
    offline["AmazonRDS"] = [
        row(
            Product_Family="Database Instance",
            Unit="Hrs",
            Instance_Type="db.m6g.large",
            Database_Engine="PostgreSQL",
            Deployment_Option=deployment,
            PricePerUnit=price,
        )
        for deployment, price in (("Single-AZ", "0.184"), ("Multi-AZ", "0.368"))
    ]
    prices = pricing.extract("AmazonRDS", "eu-west-2")

    assert prices[(Meter.RDS_INSTANCE, "db.m6g.large|PostgreSQL")] == 0.184
    assert prices[(Meter.RDS_INSTANCE_MULTI_AZ, "db.m6g.large|PostgreSQL")] == 0.368


def test_an_extended_support_surcharge_is_not_the_node_price(offline):
    """ElastiCache files the surcharge beside the node, at the same engine."""
    offline["AmazonElastiCache"] = [
        row(
            Product_Family="Cache Instance",
            Unit="Hrs",
            Instance_Type="cache.t4g.micro",
            Cache_Engine="Redis",
            usageType=usage,
            PricePerUnit=price,
        )
        for usage, price in (
            ("EUW2-NodeUsage:cache.t4g.micro", "0.018"),
            ("EUW2-ExtendedSupportYr3-NodeUsage:cache.t4g.micro", "0.029"),
        )
    ]
    prices = pricing.extract("AmazonElastiCache", "eu-west-2")
    assert prices[(Meter.ELASTICACHE_NODE, "cache.t4g.micro|Redis")] == 0.018


def test_the_ordinary_load_balancer_hour_wins(offline):
    offline["AWSELB"] = [
        row(
            Product_Family="Load Balancer-Application",
            Unit="Hrs",
            usageType=usage,
            PricePerUnit=price,
        )
        for usage, price in (
            ("EUW2-LoadBalancerUsage", "0.02646"),
            ("EUW2-Outposts-LoadBalancerUsage", "0.09"),
            ("EUW2-TS-LoadBalancerUsage", "0.0059"),
        )
    ]
    prices = pricing.extract("AWSELB", "eu-west-2")
    assert prices[(Meter.LOAD_BALANCER, "")] == 0.02646


def test_a_tiered_price_is_taken_at_its_first_paid_tier(offline):
    """The free tier is not an estimate of anything, and the volume tiers need
    volumes this tool does not ask for."""
    offline["AWSLambda"] = [
        row(
            Group="AWS-Lambda-Duration",
            Unit="Lambda-GB-Second",
            PricePerUnit="0.0",
            StartingRange="0",
        ),  # free tier
        row(
            Group="AWS-Lambda-Duration",
            Unit="Lambda-GB-Second",
            PricePerUnit="0.0000166667",
            StartingRange="0",
        ),
        row(
            Group="AWS-Lambda-Duration",
            Unit="Lambda-GB-Second",
            PricePerUnit="0.0000133334",
            StartingRange="15000000000",
        ),
    ]
    prices = pricing.extract("AWSLambda", "eu-west-2")
    assert prices[(Meter.LAMBDA_DURATION, "")] == pytest.approx(0.0000166667)


def test_two_meters_come_out_of_one_file_in_one_pass(offline):
    """EC2's file is 200 MB; the volume price is in it too, and is not worth a
    second trip through it."""
    offline["AmazonEC2"] = [
        ec2_row(),
        row(Product_Family="Storage", Volume_API_Name="gp3", Unit="GB-Mo", PricePerUnit="0.0928"),
    ]
    prices = pricing.extract("AmazonEC2", "eu-west-2")
    assert prices[(Meter.EC2_INSTANCE, "m6i.large")] == 0.111
    assert prices[(Meter.EBS_STORAGE, "")] == 0.0928


def test_a_file_nothing_is_priced_out_of_is_an_error(offline):
    with pytest.raises(pricing.PricingError):
        pricing.extract("AmazonQuantumLedgerDatabase", "eu-west-2")


def test_a_row_with_a_price_that_is_not_a_number_is_skipped(offline):
    offline["AmazonEC2"] = [ec2_row(price="on request"), ec2_row(instance_type="t3.micro")]
    prices = pricing.extract("AmazonEC2", "eu-west-2")
    assert (Meter.EC2_INSTANCE, "m6i.large") not in prices
    assert (Meter.EC2_INSTANCE, "t3.micro") in prices


# --------------------------------------------------------------------------- #
# The five lines of preamble, and the cache
# --------------------------------------------------------------------------- #


def test_the_preamble_above_the_header_is_skipped(monkeypatch):
    """AWS puts five lines of version metadata above the header row."""
    csv_text = (
        '"FormatVersion","v1.0"\n'
        '"Disclaimer","This file is intended..."\n'
        '"Publication Date","2026-08-18T00:00:00Z"\n'
        '"Version","20260818000000"\n'
        '"OfferCode","AmazonEC2"\n'
        '"SKU","TermType","PricePerUnit","Unit","Instance Type","Operating System",'
        '"Tenancy","Pre Installed S/W","CapacityStatus","StartingRange","Product Family"\n'
        '"ABC","OnDemand","0.111","Hrs","m6i.large","Linux","Shared","NA","Used","0",'
        '"Compute Instance"\n'
    )

    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

    monkeypatch.setattr(
        pricing.urllib.request, "urlopen", lambda *a, **k: FakeResponse(csv_text.encode())
    )
    prices = pricing.extract("AmazonEC2", "eu-west-2")
    assert prices == {(Meter.EC2_INSTANCE, "m6i.large"): 0.111}


def test_a_price_list_that_cannot_be_read_is_not_fatal(monkeypatch):
    def explode(*args, **kwargs):
        raise OSError("no route to host")

    monkeypatch.setattr(pricing.urllib.request, "urlopen", explode)
    with pytest.raises(pricing.PricingError):
        pricing.extract("AmazonEC2", "eu-west-2")

    # warm() reports it rather than raising: one service being unreachable
    # should not cost the estimate every other service's prices.
    outcome = pricing.warm("eu-west-2", ["AmazonEC2"])
    assert "no route to host" in str(outcome["AmazonEC2"])


def test_prices_are_fetched_once_and_then_read_from_the_store(offline):
    offline["AmazonEC2"] = [ec2_row()]
    assert pricing.warm("eu-west-2", ["AmazonEC2"]) == {"AmazonEC2": 1}

    offline.clear()  # a second fetch would now fail
    assert pricing.warm("eu-west-2", ["AmazonEC2"]) == {}
    assert store.prices_for("eu-west-2")[(Meter.EC2_INSTANCE, "m6i.large")] == 0.111


def test_prices_are_fetched_again_once_they_are_stale(offline, monkeypatch):
    offline["AmazonEC2"] = [ec2_row()]
    pricing.warm("eu-west-2", ["AmazonEC2"])

    monkeypatch.setattr(pricing, "MAX_AGE", timedelta(seconds=-1))
    offline["AmazonEC2"] = [ec2_row(price="0.222")]
    assert pricing.warm("eu-west-2", ["AmazonEC2"]) == {"AmazonEC2": 1}
    assert store.prices_for("eu-west-2")[(Meter.EC2_INSTANCE, "m6i.large")] == 0.222


def test_a_region_is_priced_on_its_own(offline):
    offline["AmazonEC2"] = [ec2_row(price="0.111")]
    pricing.warm("eu-west-2", ["AmazonEC2"])
    offline["AmazonEC2"] = [ec2_row(price="0.098")]
    pricing.warm("us-east-1", ["AmazonEC2"])

    assert store.prices_for("eu-west-2")[(Meter.EC2_INSTANCE, "m6i.large")] == 0.111
    assert store.prices_for("us-east-1")[(Meter.EC2_INSTANCE, "m6i.large")] == 0.098


# --------------------------------------------------------------------------- #
# Pricing an architecture
# --------------------------------------------------------------------------- #


@pytest.fixture
def priced(monkeypatch):
    """The sample architecture, against a known price list."""
    monkeypatch.setattr(store, "prices_for", lambda region: dict(PRICES))
    return pricing.estimate(Recommendation.model_validate_json(FULL_JSON), warm_first=False)


def test_an_architecture_adds_up_to_what_its_parts_cost(priced):
    from_the_price_list = (
        500 * 0.024  # S3
        + 730 * 0.02646  # load balancer
        + 3 * 730 * 0.111  # three application servers
        + 300 * 0.0928  # the disks under them
        + 730 * 0.169  # the cache node
        + 730 * 0.352  # the Multi-AZ database
        + 730 * 0.176  # the read replica
    )
    # CloudFront has no list price, so the advisor's own figure carries it.
    assert priced["pricedUsd"] == pytest.approx(round(from_the_price_list, 2))
    assert priced["estimatedUsd"] == pytest.approx(42.5)
    assert priced["monthlyUsd"] == pytest.approx(round(from_the_price_list + 42.5, 2))
    assert priced["region"] == "eu-west-2"


def test_the_two_halves_add_up_to_the_headline_exactly(priced):
    """Not approximately. A breakdown a penny off its total is a support ticket."""
    assert priced["pricedUsd"] + priced["estimatedUsd"] == priced["monthlyUsd"]


def test_every_service_is_accounted_for_one_way_or_the_other(priced):
    assert len(priced["services"]) == SERVICE_COUNT
    assert sum(service["priced"] for service in priced["services"]) == PRICED_SERVICE_COUNT
    assert priced["unpricedServices"] == 1

    assert priced["estimatedServices"] == 1

    cloudfront = next(s for s in priced["services"] if s["name"] == "CloudFront")
    assert cloudfront["priced"] is False
    assert cloudfront["estimated"] is True
    assert cloudfront["monthlyUsd"] == 42.5
    detail = cloudfront["lines"][0]["detail"]
    assert "not priced" in detail
    assert "the advisor's own estimate" in detail


def test_a_breakdown_line_shows_its_arithmetic(priced):
    servers = next(s for s in priced["services"] if s["name"] == "EC2 + Auto Scaling")
    instance, disk = servers["lines"]

    assert instance["detail"] == "3 x $0.1110/instance-hour x 730h"
    assert disk["detail"] == "300 x $0.0928/GB-month"


def test_a_rate_too_small_to_show_at_four_places_is_shown_at_eight(monkeypatch):
    monkeypatch.setattr(store, "prices_for", lambda region: {("lambda-requests", ""): 0.0000002})
    recommendation = Recommendation.model_validate_json(FULL_JSON)
    recommendation.services[0].usage[0].meter = Meter.LAMBDA_REQUESTS
    recommendation.services[0].usage[0].quantity = 5_000_000
    recommendation.services[0].usage[0].monthly_hours = 0

    estimate = pricing.estimate(recommendation, warm_first=False)
    assert "$0.00000020/request" in estimate["services"][0]["lines"][0]["detail"]
    assert estimate["services"][0]["monthlyUsd"] == pytest.approx(1.0)


def test_a_size_with_no_price_is_named_rather_than_guessed(monkeypatch):
    monkeypatch.setattr(store, "prices_for", lambda region: {})
    estimate = pricing.estimate(Recommendation.model_validate_json(FULL_JSON), warm_first=False)

    assert estimate["priced"] is False
    # Nothing came from AWS, but CloudFront's own figure still stands, so there
    # is a number to show. `priced` and `hasFigure` are different questions.
    assert estimate["hasFigure"] is True
    assert estimate["monthlyUsd"] == 42.5
    assert estimate["pricedUsd"] == 0.0

    line = estimate["services"][3]["lines"][0]
    assert line["priced"] is False
    assert line["estimated"] is False
    assert line["monthlyUsd"] == 0.0
    assert line["detail"] == "no list price found for m6i.large"


def test_a_size_with_no_price_takes_the_advisors_figure_when_there_is_one(monkeypatch):
    """The gap-filling half of F12, on a meter that should have had a rate."""
    monkeypatch.setattr(store, "prices_for", lambda region: {})
    recommendation = Recommendation.model_validate_json(FULL_JSON)
    recommendation.services[3].usage[0].estimated_monthly_usd = 260.0

    estimate = pricing.estimate(recommendation, warm_first=False)
    line = estimate["services"][3]["lines"][0]

    assert line["priced"] is False
    assert line["estimated"] is True
    assert line["monthlyUsd"] == 260.0
    assert "no list price found for m6i.large" in line["detail"]
    assert "$260.0000/month is the advisor's own estimate" in line["detail"]


def test_a_list_price_beats_the_advisors_figure_for_the_same_line(monkeypatch):
    """A rate is a fact and an estimate is a judgement. The fact wins outright."""
    monkeypatch.setattr(store, "prices_for", lambda region: dict(PRICES))
    recommendation = Recommendation.model_validate_json(FULL_JSON)
    # A wild figure on a line that prices perfectly well.
    recommendation.services[1].usage[0].estimated_monthly_usd = 99_999.0

    estimate = pricing.estimate(recommendation, warm_first=False)
    s3 = next(s for s in estimate["services"] if s["name"] == "S3")

    assert s3["priced"] is True
    assert s3["estimated"] is False
    assert s3["monthlyUsd"] == pytest.approx(500 * 0.024)
    assert "99,999" not in s3["lines"][0]["detail"]


def test_an_architecture_with_no_prices_and_no_estimates_has_no_figure(monkeypatch):
    """What a conversation stored before F12 looks like: zero is not an answer."""
    monkeypatch.setattr(store, "prices_for", lambda region: {})
    recommendation = Recommendation.model_validate_json(FULL_JSON)
    for service in recommendation.services:
        for line in service.usage:
            line.estimated_monthly_usd = 0.0

    estimate = pricing.estimate(recommendation, warm_first=False)

    assert estimate["priced"] is False
    assert estimate["anyEstimated"] is False
    assert estimate["hasFigure"] is False
    assert estimate["monthlyUsd"] == 0.0
    assert estimate["linesEstimated"] == 0
    assert estimate["linesPriced"] == 0
    assert estimate["linesTotal"] == 8


def test_an_engine_this_region_does_not_sell_falls_back_and_says_so(monkeypatch):
    """A number for the wrong engine, labelled, beats no number at all."""
    monkeypatch.setattr(
        store,
        "prices_for",
        lambda region: {("rds-instance", "db.m6g.large|MariaDB"): 0.176},
    )
    recommendation = Recommendation.model_validate_json(FULL_JSON)
    estimate = pricing.estimate(recommendation, warm_first=False)

    replica = next(s for s in estimate["services"] if s["name"] == "RDS Read Replica")
    assert replica["priced"] is True
    assert "priced as db.m6g.large MariaDB" in replica["lines"][0]["detail"]


def test_an_hourly_line_with_no_hours_is_treated_as_always_on(monkeypatch):
    monkeypatch.setattr(store, "prices_for", lambda region: dict(PRICES))
    recommendation = Recommendation.model_validate_json(FULL_JSON)
    servers = recommendation.services[3]
    servers.usage[0].monthly_hours = 0

    estimate = pricing.estimate(recommendation, warm_first=False)
    priced_servers = estimate["services"][3]["lines"][0]
    assert priced_servers["monthlyUsd"] == pytest.approx(3 * 730 * 0.111)


@pytest.mark.parametrize(
    ("monthly", "tier"),
    [(0.0, "Low"), (499.99, "Low"), (500.0, "Medium"), (4_999.0, "Medium"), (5_000.0, "High")],
)
def test_a_figure_is_put_back_into_the_model_s_own_words(monthly, tier):
    assert pricing.tier_for(monthly) == tier


def test_the_estimate_keeps_the_tier_the_model_claimed(priced):
    """F1: the model's guess stays in the data, as the sanity check."""
    assert priced["claimedTier"] == "Medium"
    assert priced["tier"] == pricing.tier_for(priced["monthlyUsd"])
    # $853 is Medium and the model said Medium, so there is nothing to warn about.
    assert priced["tierGap"] == 0


@pytest.mark.parametrize(
    ("claimed", "computed", "gap"),
    [
        # Agreement, exact.
        ("Low", "Low", 0),
        ("Medium", "Medium", 0),
        ("High", "High", 0),
        # A straddled claim landing in one of the two tiers it straddles is
        # agreement too, which is the whole reason the half-band scale exists.
        ("Low\u2013Medium", "Low", 1),
        ("Low\u2013Medium", "Medium", 1),
        ("Medium\u2013High", "Medium", 1),
        ("Medium\u2013High", "High", 1),
        # A full band apart, and worth saying out loud.
        ("Low", "Medium", 2),
        ("Medium", "Low", 2),
        ("High", "Medium", 2),
        ("Low\u2013Medium", "High", 3),
        ("Low", "High", 4),
        # A tier neither vocabulary can name is not a disagreement.
        ("", "Medium", 0),
        ("Enormous", "Low", 0),
    ],
)
def test_the_two_cost_vocabularies_are_differenced_not_matched(claimed, computed, gap):
    assert pricing.tier_gap(claimed, computed) == gap


def test_a_gap_worth_saying_is_a_full_band(priced):
    """The threshold the callers warn on, named once rather than five times."""
    assert pricing.TIER_GAP_WORTH_SAYING == 2
    assert pricing.tier_gap("Low", "Medium") >= pricing.TIER_GAP_WORTH_SAYING
    assert pricing.tier_gap("Low\u2013Medium", "Medium") < pricing.TIER_GAP_WORTH_SAYING


def test_an_all_estimated_total_never_argues_with_the_model(monkeypatch):
    """Nothing came from AWS, so there is no second opinion to disagree with."""
    monkeypatch.setattr(store, "prices_for", lambda region: {})
    recommendation = Recommendation.model_validate_json(FULL_JSON)
    recommendation.services[0].usage[0].estimated_monthly_usd = 40_000.0

    estimate = pricing.estimate(recommendation, warm_first=False)

    assert estimate["tier"] == "High"
    assert estimate["claimedTier"] == "Medium"
    assert estimate["pricedUsd"] == 0.0
    assert estimate["tierGap"] == 0


def test_only_the_files_an_architecture_needs_are_fetched(monkeypatch):
    asked: list[str] = []

    def warm(region, offer_codes=None):
        asked.extend(offer_codes or [])
        return {}

    monkeypatch.setattr(pricing, "warm", warm)
    monkeypatch.setattr(store, "prices_for", lambda region: dict(PRICES))

    recommendation = Recommendation.model_validate_json(FULL_JSON)
    recommendation.services = recommendation.services[:2]  # CloudFront and S3
    pricing.estimate(recommendation)

    # CloudFront is unpriced, so its file is not one there is any point pulling.
    assert asked == ["AmazonS3"]


# --------------------------------------------------------------------------- #
# Warming from the command line
# --------------------------------------------------------------------------- #


def test_the_command_line_reports_what_it_pulled(offline, capsys):
    for offer_code in pricing.BY_OFFER:
        offline[offer_code] = []
    offline["AmazonEC2"] = [ec2_row()]
    offline["AmazonS3"] = [row(Volume_Type="Standard", Unit="GB-Mo", PricePerUnit="0.024")]

    assert pricing.main(["--warm", "eu-west-2"]) == 0
    output = capsys.readouterr().out
    assert "AmazonEC2: 1 rates" in output
    assert "AmazonS3: 1 rates" in output


def test_a_price_list_that_cannot_be_read_is_reported_and_not_hidden(offline, capsys):
    offline["AmazonEC2"] = [ec2_row()]  # every other file is unreachable

    assert pricing.main(["--warm", "eu-west-2"]) == 1
    output = capsys.readouterr().out
    assert "AmazonEC2: 1 rates" in output
    assert "nothing stubbed for AmazonRDS" in output


def test_a_region_this_does_not_price_is_refused(capsys):
    assert pricing.main(["--warm", "mars-north-1"]) == 1
    assert "not one of the regions" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# The real price list (opt-in: `pytest -m network`)
# --------------------------------------------------------------------------- #


@pytest.mark.network
@pytest.mark.parametrize("offer_code", sorted(pricing.BY_OFFER))
def test_the_real_price_list_still_answers_every_meter(offer_code):
    """The filters here are pinned to column values AWS chose, and can move.

    This is the test that notices. It is opt-in because it downloads a couple of
    hundred megabytes to do it.
    """
    prices = pricing.extract(offer_code, "eu-west-2")
    found = {meter for meter, _ in prices}
    assert found == set(pricing.BY_OFFER[offer_code]), (
        f"{offer_code} no longer answers every meter taken from it"
    )
    assert all(price > 0 for price in prices.values())


@pytest.mark.network
def test_a_known_london_price_is_still_what_it_was():
    """A canary: if this moves, either AWS changed a price or a filter is wrong."""
    prices = pricing.extract("AWSELB", "eu-west-2")
    assert prices[(Meter.LOAD_BALANCER, "")] == pytest.approx(0.02646, rel=0.5)


# --------------------------------------------------------------------------- #
# One fetch at a time, and a bound on the waiting (P1)
# --------------------------------------------------------------------------- #


def test_two_callers_wanting_the_same_file_share_one_fetch(offline, monkeypatch):
    """P1. The web app serves on several threads and this file is 210 MB.

    Two tabs asking for a first architecture in the same region both found the
    prices stale, both started the same walk, and both wrote the same rows --
    charging the wait to both readers and the egress to AWS twice. The freshness
    check is repeated inside the lock, so the caller that waited finds what the
    other one cached.
    """
    inside = threading.Event()
    release = threading.Event()
    fetches = []

    def rows(offer_code, region):
        fetches.append(offer_code)
        inside.set()
        # Held open so the second caller is definitely at the lock while the
        # first is still fetching. Without the lock it fetches too.
        assert release.wait(timeout=10)
        return iter([ec2_row()])

    monkeypatch.setattr(pricing, "_rows", rows)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(pricing.warm, "eu-west-2", ["AmazonEC2"])
        assert inside.wait(timeout=10), "the first caller never started fetching"
        second = pool.submit(pricing.warm, "eu-west-2", ["AmazonEC2"])
        time.sleep(0.2)  # long enough for the second to reach the lock and wait
        release.set()
        outcomes = [first.result(timeout=15), second.result(timeout=15)]

    # One fetch, not two.
    assert fetches == ["AmazonEC2"]
    assert outcomes[0] == {"AmazonEC2": 1}
    # The one that waited found the rows the other had just cached, so it has
    # nothing of its own to report.
    assert outcomes[1] == {}
    assert store.prices_for("eu-west-2")[(Meter.EC2_INSTANCE, "m6i.large")] == 0.111


def test_a_lock_is_kept_per_file_and_per_region(offline):
    """Two different files, or two different regions, do not wait on each other."""
    london = pricing._fetch_lock("AmazonEC2", "eu-west-2")

    assert pricing._fetch_lock("AmazonEC2", "eu-west-2") is london
    assert pricing._fetch_lock("AmazonRDS", "eu-west-2") is not london
    assert pricing._fetch_lock("AmazonEC2", "eu-west-1") is not london


def test_warming_gives_up_rather_than_holding_a_request_open(offline, monkeypatch):
    """P1. Five unreachable files at FETCH_TIMEOUT each is a quarter of an hour.

    The deadline bounds the whole call, not any one file, so what has not been
    reached yet is reported as skipped instead of waited for.
    """
    slow = []

    def rows(offer_code, region):
        slow.append(offer_code)
        time.sleep(0.05)
        return iter([ec2_row()])

    monkeypatch.setattr(pricing, "_rows", rows)

    outcome = pricing.warm("eu-west-2", ["AmazonEC2", "AmazonRDS", "AmazonS3"], deadline=0.0)

    # The first is let through -- the deadline is checked before a fetch, and
    # nothing has been spent yet -- and the rest are named rather than fetched.
    assert slow == ["AmazonEC2"]
    for code in ("AmazonRDS", "AmazonS3"):
        assert "Gave up" in str(outcome[code])


def test_what_could_not_be_fetched_is_reported_to_the_estimate(offline, monkeypatch):
    """A skipped file is a problem the estimate carries, not a silent zero."""
    monkeypatch.setattr(pricing, "_rows", lambda code, region: iter([ec2_row()]))
    recommendation = Recommendation.model_validate_json(FULL_JSON)

    monkeypatch.setattr(pricing, "WARM_DEADLINE", 0.0)
    estimate = pricing.estimate(recommendation)

    # Whatever was reached is priced; whatever was not says so.
    assert estimate["hasFigure"] is True
    assert isinstance(estimate["problems"], list)
