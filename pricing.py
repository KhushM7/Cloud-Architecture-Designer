"""What an architecture actually costs, from the AWS Price List.

The cost tier the model gives is a guess. It is presented in an accented panel as
a monthly figure, which is the least defensible part of the output and the first
thing a client will challenge. This module prices the architecture instead: the
model says what it would run and how much of it, and those lines are looked up
in AWS's own published prices.

Where the prices come from
--------------------------
The bulk Price List, which is a plain HTTPS GET and needs no AWS credentials:

    https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/<offer>/current/<region>/index.csv

One file per service per region. They are large -- EC2 in London is 210 MB and
276,000 rows -- so they are streamed and filtered a row at a time rather than
loaded, and what survives is a few hundred prices that go into the store. A
region's EC2 file takes about twelve seconds to walk; everything after that is a
lookup. `python pricing.py --warm eu-west-2` does it ahead of time.

What is priced, and what is not
-------------------------------
The ten meters in schema.py, which between them cover most of what one of these
architectures is billed for. A service that fits none of them -- CloudFront,
Route 53, a NAT gateway -- has no list price to find, and used to be shown as
unpriced and counted as nothing, which made the total a floor rather than an
answer.

It is now a blend (F12). The model writes its own monthly figure for every line
it sizes, and where there is no list price to find, that figure is what the line
carries. So each line ends up in one of three states, and which one it is in is
never hidden:

    priced      the AWS Price List had a rate. The rate wins, always.
    estimated   no list price, and the model gave a figure. That figure is used.
    neither     no list price and no figure. Counted as nothing, and named.

The third state is what a conversation stored before F12 looks like, and it is
why "estimated" is a flag of its own rather than the absence of "priced": a zero
estimate is not the same claim as a fifty-dollar one, and printing $0.00 as
though it were an answer is the failure this module exists to avoid.

`monthlyUsd` is the one figure to quote, and `pricedUsd` and `estimatedUsd` are
the two halves it is made of. The band a reader is shown comes from that total
via tier_for(), so the band and the figure cannot contradict each other. The
model's own tier is kept beside them, not to be displayed, but to be differenced
against the arithmetic -- see tier_gap(), which is what catches a mis-sized
architecture.

Everything here is on-demand list price in US dollars, before any discount,
Savings Plan, free tier or tiered rate. AWS bills in dollars and the model is
asked for dollars, so no exchange rate is invented anywhere.
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime, timedelta
from typing import Any, NamedTuple

import store
from schema import MONTHLY_HOURS, Meter, Recommendation, Region, Tier

log = logging.getLogger("advisor.pricing")

PRICE_LIST = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws"

# The bulk files carry five lines of preamble before the header row.
CSV_PREAMBLE = 5

# Per read once streaming has started, not per file: a stalled connection is what
# this catches, and a 210 MB walk is expected to take far longer than this in
# total. WARM_DEADLINE below is the bound on the total.
FETCH_TIMEOUT = 180.0

# The longest warm() will spend fetching before it gives up on the files it has
# not reached yet. Without it, five unreachable price lists at FETCH_TIMEOUT each
# hold an /api/estimate request open for a quarter of an hour, with the browser
# saying "Pricing this against the AWS Price List..." the whole time. A partial
# answer now beats a complete one nobody waited for; what could not be fetched
# comes back in `problems` and the lines it would have priced fall back to the
# advisor's own figures.
WARM_DEADLINE = 240.0

# How long a cached price is trusted. AWS changes list prices rarely, and a
# week-old figure is a better answer than a minute of waiting on every request.
MAX_AGE = timedelta(days=7)


class Offer(NamedTuple):
    """One meter, and how to find it in the price list file it lives in.

    `code` is AWS's name for the file. `keep` picks the rows that are this
    meter, out of a file that holds every way the service can be charged.
    `key` is what a row is looked up by afterwards -- an instance type, an
    engine, or nothing at all for a service with a single price.
    """

    code: str
    unit: str
    keep: Callable[[Mapping[str, str]], bool]
    key: Callable[[Mapping[str, str]], str] = lambda row: ""
    hourly: bool = True


def _text(row: Mapping[str, str], column: str) -> str:
    return (row.get(column) or "").strip()


def _rate(price: float) -> str:
    """A unit price, at enough precision to be worth printing.

    Four decimal places suits an instance-hour and renders a Lambda request --
    two ten-millionths of a dollar -- as zero, which is worse than useless in a
    breakdown meant to be checked.
    """
    return f"${price:,.4f}" if price >= 0.001 else f"${price:,.8f}"


def _node_usage(row: Mapping[str, str]) -> bool:
    """A plain running node, not one of the surcharges filed beside it.

    ElastiCache lists extended-support charges for the same instance type and
    engine, at a different price, with nothing but the usage type to tell them
    apart.
    """
    usage = _text(row, "usageType")
    return "-NodeUsage:" in usage and "ExtendedSupport" not in usage


def _plain_load_balancer(row: Mapping[str, str]) -> bool:
    """The ordinary hourly ALB charge, not the Outposts or Time Series variant."""
    usage = _text(row, "usageType")
    return usage.endswith("-LoadBalancerUsage") and "Outposts" not in usage and "-TS-" not in usage


OFFERS: dict[str, Offer] = {
    Meter.EC2_INSTANCE: Offer(
        code="AmazonEC2",
        unit="instance-hour",
        # One instance type is priced a dozen ways -- by operating system, by
        # tenancy, by whatever is pre-installed on it. This is the plain one.
        keep=lambda row: (
            _text(row, "Operating System") == "Linux"
            and _text(row, "Tenancy") == "Shared"
            and _text(row, "Pre Installed S/W") == "NA"
            and _text(row, "CapacityStatus") == "Used"
            and _text(row, "Unit") == "Hrs"
        ),
        key=lambda row: _text(row, "Instance Type"),
    ),
    Meter.EBS_STORAGE: Offer(
        code="AmazonEC2",
        unit="GB-month",
        keep=lambda row: (
            _text(row, "Product Family") == "Storage"
            and _text(row, "Volume API Name") == "gp3"
            and _text(row, "Unit") == "GB-Mo"
        ),
        hourly=False,
    ),
    Meter.RDS_INSTANCE: Offer(
        code="AmazonRDS",
        unit="instance-hour",
        keep=lambda row: (
            _text(row, "Product Family") == "Database Instance"
            and _text(row, "Unit") == "Hrs"
            and _text(row, "Deployment Option") == "Single-AZ"
        ),
        key=lambda row: f"{_text(row, 'Instance Type')}|{_text(row, 'Database Engine')}",
    ),
    # A standby in a second availability zone is a second instance on the bill,
    # and AWS prices it as its own rate rather than as twice the first one. It
    # is the difference between a right answer and one that is half the size.
    Meter.RDS_INSTANCE_MULTI_AZ: Offer(
        code="AmazonRDS",
        unit="instance-hour",
        keep=lambda row: (
            _text(row, "Product Family") == "Database Instance"
            and _text(row, "Unit") == "Hrs"
            and _text(row, "Deployment Option") == "Multi-AZ"
        ),
        key=lambda row: f"{_text(row, 'Instance Type')}|{_text(row, 'Database Engine')}",
    ),
    Meter.ELASTICACHE_NODE: Offer(
        code="AmazonElastiCache",
        unit="node-hour",
        keep=lambda row: (
            _text(row, "Product Family") == "Cache Instance"
            and _text(row, "Unit") == "Hrs"
            and _node_usage(row)
        ),
        key=lambda row: f"{_text(row, 'Instance Type')}|{_text(row, 'Cache Engine')}",
    ),
    Meter.LOAD_BALANCER: Offer(
        code="AWSELB",
        unit="load-balancer-hour",
        keep=lambda row: (
            _text(row, "Product Family") == "Load Balancer-Application"
            and _text(row, "Unit") == "Hrs"
            and _plain_load_balancer(row)
        ),
    ),
    Meter.S3_STORAGE: Offer(
        code="AmazonS3",
        unit="GB-month",
        keep=lambda row: _text(row, "Volume Type") == "Standard" and _text(row, "Unit") == "GB-Mo",
        hourly=False,
    ),
    Meter.LAMBDA_REQUESTS: Offer(
        code="AWSLambda",
        unit="request",
        keep=lambda row: (
            _text(row, "Group") == "AWS-Lambda-Requests"
            and _text(row, "Unit").startswith("Request")
        ),
        hourly=False,
    ),
    Meter.LAMBDA_DURATION: Offer(
        code="AWSLambda",
        unit="GB-second",
        keep=lambda row: (
            _text(row, "Group") == "AWS-Lambda-Duration"
            and _text(row, "Unit") == "Lambda-GB-Second"
        ),
        hourly=False,
    ),
}

# Which meters come out of which file, so a 210 MB download is walked once for
# everything in it rather than once per meter.
BY_OFFER: dict[str, list[str]] = {}
for _meter, _offer in OFFERS.items():
    BY_OFFER.setdefault(_offer.code, []).append(_meter)


# --------------------------------------------------------------------------- #
# Reading the price list
# --------------------------------------------------------------------------- #


class PricingError(Exception):
    """The price list could not be read. Never fatal: an estimate is optional."""


def _rows(offer_code: str, region: str) -> Iterator[dict[str, str]]:
    """Stream one price list file, a row at a time.

    Streamed rather than downloaded because these files reach 200 MB and only a
    few hundred rows of any of them are ever wanted.
    """
    url = f"{PRICE_LIST}/{offer_code}/current/{region}/index.csv"
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT) as response:
            stream = io.TextIOWrapper(response, encoding="utf-8", newline="")
            for _ in range(CSV_PREAMBLE):
                stream.readline()
            yield from csv.DictReader(stream)
    except (urllib.error.URLError, OSError, csv.Error) as e:
        raise PricingError(f"Could not read the {offer_code} price list for {region}: {e}") from e


def _starting_range(row: Mapping[str, str]) -> float:
    """Where a tiered row starts. Rows that are not tiered start at zero."""
    try:
        return float(_text(row, "StartingRange") or 0)
    except ValueError:
        return 0.0


def extract(offer_code: str, region: str) -> dict[tuple[str, str], float]:
    """Walk one price list file and pull out every meter it holds.

    Returns {(meter, key): price}. Where a meter is tiered -- S3 storage, Lambda
    duration -- the first paid tier wins: the free tier is not an estimate of
    anything, and the volume discounts further up need volumes this tool does
    not ask for. Both make the total a floor rather than a fiction.
    """
    meters = BY_OFFER.get(offer_code, [])
    if not meters:
        raise PricingError(f"Nothing is priced out of {offer_code}.")

    found: dict[tuple[str, str], tuple[float, float]] = {}
    for row in _rows(offer_code, region):
        if _text(row, "TermType") != "OnDemand":
            continue
        try:
            price = float(_text(row, "PricePerUnit") or 0)
        except ValueError:
            continue
        if price <= 0:  # a free tier, or a row that carries no price at all
            continue

        for meter in meters:
            offer = OFFERS[meter]
            if not offer.keep(row):
                continue
            entry = (meter, offer.key(row))
            tier = _starting_range(row)
            seen = found.get(entry)
            if seen is None or tier < seen[0]:
                found[entry] = (tier, price)

    return {entry: price for entry, (_, price) in found.items()}


def refresh(offer_code: str, region: str) -> int:
    """Pull one price list file into the store. Returns how many prices landed."""
    started = datetime.now()
    prices = extract(offer_code, region)
    store.save_prices(offer_code, region, prices)
    log.info(
        "priced %s in %s: %s rates in %.1fs",
        offer_code,
        region,
        len(prices),
        (datetime.now() - started).total_seconds(),
    )
    return len(prices)


# One in-flight fetch per price list file per region. The web app serves on
# several threads, so two tabs asking for a first architecture in the same region
# would otherwise both find the prices stale, both start the same 210 MB walk,
# and both write the same rows -- charging the wait to both readers and the
# egress to AWS twice. Four tabs, four walks.
#
# The lock is only ever held around the fetch, never around a read: `prices_for`
# and every other query stays free, and WAL means a read runs while the write
# that follows a fetch is in progress.
_fetch_locks: dict[tuple[str, str], threading.Lock] = {}
_locks_guard = threading.Lock()


def _fetch_lock(offer_code: str, region: str) -> threading.Lock:
    """The lock for one price list file in one region, made on first ask."""
    key = (offer_code, region)
    with _locks_guard:
        lock = _fetch_locks.get(key)
        if lock is None:
            lock = _fetch_locks[key] = threading.Lock()
    return lock


def warm(
    region: str, offer_codes: list[str] | None = None, deadline: float = WARM_DEADLINE
) -> dict[str, int | str]:
    """Make sure the prices for a region are on hand, fetching what is stale.

    Each file is reported on separately: one service being unreachable should
    not cost the estimate every other service's prices.

    Two callers wanting the same file wait for one fetch rather than both making
    it, and the freshness check is repeated inside the lock -- so the one that
    waited finds the rows the other just cached and returns without fetching.

    `deadline` bounds the whole call rather than any one file. Past it the
    remaining files are reported as skipped instead of fetched, which keeps an
    unreachable price list from holding a request open for minutes.
    """
    outcome: dict[str, int | str] = {}
    started = time.monotonic()
    attempted = 0

    for code in offer_codes or sorted(BY_OFFER):
        if store.prices_are_fresh(code, region, MAX_AGE):
            continue

        # Checked only once something has actually been spent, so the first file
        # is always tried: a deadline is a bound on waiting for the ones after
        # it, not a reason to answer a single-file warm with nothing.
        waited = time.monotonic() - started
        if attempted and waited > deadline:
            outcome[code] = (
                f"Gave up before reading the {code} price list for {region}: "
                f"{waited:.0f}s spent already, and the limit is {deadline:.0f}s."
            )
            log.warning("%s", outcome[code])
            continue
        attempted += 1

        with _fetch_lock(code, region):
            # Someone else may have fetched this while we waited for the lock.
            if store.prices_are_fresh(code, region, MAX_AGE):
                continue
            try:
                outcome[code] = refresh(code, region)
            except PricingError as e:
                log.warning("%s", e)
                outcome[code] = str(e)
    return outcome


# --------------------------------------------------------------------------- #
# Pricing a recommendation
# --------------------------------------------------------------------------- #

# What the computed monthly total means in the model's own terms. This is the
# band a reader is shown, derived from the figure beside it so the two cannot
# disagree. The boundaries are house rules -- round numbers that read sensibly to
# somebody who is not going to check them -- and not anything AWS publishes.
TIER_BANDS = ((500.0, "Low"), (5_000.0, "Medium"))


def tier_for(monthly_usd: float) -> str:
    """The tier a monthly figure falls in, in the model's vocabulary."""
    for ceiling, tier in TIER_BANDS:
        if monthly_usd < ceiling:
            return tier
    return "High"


# Both vocabularies on one scale, so "a band apart" is arithmetic rather than a
# substring test. tier_for() speaks three words; the model can also straddle two,
# and a straddled tier sits between the bands it straddles. That is what makes it
# worth having: a model that says Low-Medium and prices to Medium landed next
# door, not in the wrong place.
TIER_SCALE: dict[str, int] = {
    Tier.LOW: 0,
    Tier.LOW_MEDIUM: 1,
    Tier.MEDIUM: 2,
    Tier.MEDIUM_HIGH: 3,
    Tier.HIGH: 4,
}

# Two half-bands apart: a full band of disagreement, and the point at which the
# model has mis-sized something rather than rounded it differently.
TIER_GAP_WORTH_SAYING = 2


def tier_gap(claimed: str, computed: str) -> int:
    """How far apart the model's band and the arithmetic's are, in half-bands.

    0 or 1 is agreement -- 1 being a straddled claim landing in one of the two
    tiers it straddles. 2 or more is a full band apart and worth saying out loud.
    A tier neither side can name scores 0: an unreadable claim is not a
    disagreement.
    """
    left, right = TIER_SCALE.get(claimed), TIER_SCALE.get(computed)
    if left is None or right is None:
        return 0
    return abs(left - right)


def _lookup(prices: Mapping[tuple[str, str], float], meter: str, key: str) -> tuple[str, float]:
    """Find a price, falling back to the cheapest variant of the same size.

    A model that asks for `db.m6g.large` on an engine this region does not sell
    should still get a number, as long as it is clear which engine it is for.
    Returns the key actually priced, so the breakdown can say so.
    """
    exact = prices.get((meter, key))
    if exact is not None:
        return key, exact

    size = key.split("|")[0]
    if not size:
        return key, 0.0
    alternatives = {
        candidate: price
        for (candidate_meter, candidate), price in prices.items()
        if candidate_meter == meter and candidate.split("|")[0] == size
    }
    if not alternatives:
        return key, 0.0
    cheapest = min(alternatives, key=lambda candidate: alternatives[candidate])
    return cheapest, alternatives[cheapest]


def _fallback(usage: Any, reason: str) -> dict[str, Any]:
    """A line the price list could not answer, carrying the model's own figure.

    Where the model gave one. Where it did not -- a zero, or a reply stored
    before the field existed -- the line is neither priced nor estimated, and
    counts as nothing. `reason` keeps its old wording so the breakdown still
    says which of the two kinds of miss this was.
    """
    figure = max(0.0, float(getattr(usage, "estimated_monthly_usd", 0.0) or 0.0))
    if not figure:
        return {
            "meter": str(usage.meter),
            "priced": False,
            "estimated": False,
            "monthlyUsd": 0.0,
            "detail": reason,
        }
    return {
        "meter": str(usage.meter),
        "priced": False,
        "estimated": True,
        "monthlyUsd": figure,
        "detail": f"{reason}; {_rate(figure)}/month is the advisor's own estimate",
    }


def _line(usage: Any, prices: Mapping[tuple[str, str], float]) -> dict[str, Any]:
    """Price one line of usage, falling back to the model's figure.

    The list price wins wherever there is one. That order matters: the model's
    figure is a judgement and the rate is a fact, and the only reason to hold
    both is so the fact can displace the judgement rather than average with it.
    """
    meter = str(usage.meter)
    if meter == Meter.UNPRICED:
        return _fallback(usage, "not priced here")

    offer = OFFERS[meter]
    key = f"{usage.size}|{usage.variant}" if usage.variant else usage.size
    priced_as, rate = _lookup(prices, meter, key)

    if not rate:
        return _fallback(usage, f"no list price found for {usage.size or offer.code}")

    hours = usage.monthly_hours if offer.hourly else 1.0
    if offer.hourly and hours <= 0:
        hours = MONTHLY_HOURS
    monthly = rate * max(0.0, usage.quantity) * hours

    quantity = f"{usage.quantity:,.0f}" if usage.quantity >= 10 else f"{usage.quantity:g}"
    detail = f"{quantity} x {_rate(rate)}/{offer.unit}"
    if offer.hourly:
        detail += f" x {hours:,.0f}h"
    if priced_as != key:
        detail += f" (priced as {priced_as.replace('|', ' ')})"

    return {
        "meter": meter,
        "priced": True,
        "estimated": False,
        "monthlyUsd": monthly,
        "detail": detail,
    }


def estimate(recommendation: Recommendation, warm_first: bool = True) -> dict[str, Any]:
    """Price a recommendation against the AWS Price List.

    Returns the per-service breakdown, the one monthly total, the two halves it
    is made of, and what neither source could answer. Nothing here raises: an
    architecture with no prices behind it still renders, saying so.
    """
    region = str(recommendation.region)
    needed = sorted(
        {
            OFFERS[str(line.meter)].code
            for service in recommendation.services
            for line in service.usage
            if str(line.meter) != Meter.UNPRICED
        }
    )

    problems: list[str] = []
    if warm_first and needed:
        for code, result in warm(region, needed).items():
            if isinstance(result, str):
                problems.append(f"{code}: {result}")

    prices = store.prices_for(region)
    services = []
    priced_usd = 0.0
    estimated_usd = 0.0
    unpriced = 0
    estimated_services = 0
    lines_priced = 0
    lines_estimated = 0
    lines_total = 0

    for service in recommendation.services:
        lines = [_line(line, prices) for line in service.usage]
        from_list = any(line["priced"] for line in lines)
        from_model = any(line["estimated"] for line in lines)

        for line in lines:
            if line["priced"]:
                priced_usd += line["monthlyUsd"]
            elif line["estimated"]:
                estimated_usd += line["monthlyUsd"]

        lines_total += len(lines)
        lines_priced += sum(1 for line in lines if line["priced"])
        lines_estimated += sum(1 for line in lines if line["estimated"])
        if not from_list:
            unpriced += 1
        if from_model:
            estimated_services += 1

        services.append(
            {
                "name": service.name,
                "monthlyUsd": sum(line["monthlyUsd"] for line in lines),
                "priced": from_list,
                "estimated": from_model,
                "lines": lines,
            }
        )

    # Round the halves before adding them, so the breakdown adds up to the
    # headline exactly. A total that is a penny off its own parts is a support
    # ticket, and the arithmetic is the one thing here nobody should have to
    # take on trust.
    priced_usd = round(priced_usd, 2)
    estimated_usd = round(estimated_usd, 2)
    total = round(priced_usd + estimated_usd, 2)

    # A total made entirely of the model's own figures cannot disagree with the
    # model, so there is nothing to warn about: the app would be arguing with
    # itself. The check needs at least some real money behind it to mean
    # anything.
    claimed = recommendation.cost.tier.value
    gap = tier_gap(claimed, tier_for(total)) if priced_usd > 0 else 0

    return {
        "region": region,
        "monthlyUsd": total,
        "pricedUsd": priced_usd,
        "estimatedUsd": estimated_usd,
        "tier": tier_for(total),
        "claimedTier": claimed,
        "tierGap": gap,
        "services": services,
        "unpricedServices": unpriced,
        "estimatedServices": estimated_services,
        "linesPriced": lines_priced,
        "linesEstimated": lines_estimated,
        "linesTotal": lines_total,
        # `priced` still means what it always did: some of this came from AWS.
        # `hasFigure` is the one to gate a display on -- whether there is a
        # number at all, wherever it came from.
        "priced": any(service["priced"] for service in services),
        "anyEstimated": estimated_usd > 0,
        "hasFigure": total > 0,
        "problems": problems,
        "pricedAt": datetime.now(store.UK).isoformat(),
    }


# --------------------------------------------------------------------------- #
# Warming the cache from the command line
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python pricing.py",
        description="Pull AWS list prices into the local store, so an estimate is instant.",
    )
    parser.add_argument(
        "--warm",
        metavar="REGION",
        default=Region.LONDON.value,
        help=f"which region to price (default {Region.LONDON.value})",
    )
    parser.add_argument(
        "--force", action="store_true", help="fetch again even if the prices are fresh"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.warm not in {region.value for region in Region}:
        print(f"{args.warm} is not one of the regions this prices: ")
        print("  " + ", ".join(region.value for region in Region))
        return 1

    if args.force:
        store.forget_prices(args.warm)

    print(f"Pricing {args.warm}. The EC2 file is the slow one, at around 200 MB.")
    outcome = warm(args.warm)
    if not outcome:
        print("Already up to date.")
        return 0

    failed = 0
    for code, result in sorted(outcome.items()):
        if isinstance(result, str):
            failed += 1
            print(f"  {code}: {result}")
        else:
            print(f"  {code}: {result} rates")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
