"""Architecture Advisor - command line interface."""

import argparse
import io
import logging
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from itertools import zip_longest

# Emoji in the replies need a UTF-8 console; Windows still defaults to cp1252.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure") and (_stream.encoding or "").lower() != "utf-8":
        _stream.reconfigure(encoding="utf-8")

from anthropic import Anthropic
from anthropic.types import MessageParam
from rich.cells import cell_len
from rich.console import Console
from rich.markdown import Markdown

import branding
import pricing
import store
from advisor import (
    AdvisorError,
    Progress,
    Reply,
    advise,
    call_claude,
    constraints_line,
    get_client,
    merge_usage,
)
from schema import Compliance, Region

log = logging.getLogger("advisor.cli")

EXIT_WORDS = {"exit", "quit", "q"}

# How many architectures one comparison may weigh up (F9). The same two numbers
# server.py enforces, because a comparison is the same job either way round.
MIN_COMPARE = 2
MAX_COMPARE = 4

# What each column is called while it is being asked for. Only the first four
# are ever used, because MAX_COMPARE is four.
ORDINALS = ("one", "two", "three", "four")

DESCRIPTION = """\
Describe an AWS workload in plain English and get back a structured architecture
recommendation: a services table, Well-Architected notes, and a cost tier.
"""

EPILOG = """\
examples:
  python main.py
      Start an interactive advisory session, then ask follow-up questions.

  python main.py -w "A patient records system for an NHS trust, 500 concurrent users"
      Skip the prompt and describe the workload up front.

  python main.py --compare
      Enter two workloads and see the two architectures side by side.

  python main.py --compare -w "Serverless IoT pipeline" -w "Same pipeline on EC2"
      Compare two workloads without any prompting.

Set ANTHROPIC_API_KEY in your environment or a .env file before running.
"""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-w",
        "--workload",
        action="append",
        metavar="TEXT",
        help="workload description; pass twice with --compare (prompts if omitted)",
    )
    parser.add_argument(
        "-c",
        "--compare",
        action="store_true",
        help=f"compare {MIN_COMPARE} to {MAX_COMPARE} workloads side by side",
    )
    parser.add_argument(
        "--region",
        choices=[region.value for region in Region],
        help="where the architecture must run (the advisor chooses if omitted)",
    )
    parser.add_argument(
        "--compliance",
        choices=[item.value for item in Compliance],
        default=Compliance.NONE.value,
        help="a regime the architecture has to stand up to",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="do not store the conversation",
    )
    args = parser.parse_args(argv)

    given = len(args.workload or [])
    expected = MAX_COMPARE if args.compare else 1
    if given > expected:
        parser.error(f"expected at most {expected} --workload argument(s), got {given}")
    # Two is the least that is a comparison. Fewer on the command line means the
    # rest are prompted for, so only an explicit single -w is a mistake worth
    # naming; nothing at all is the interactive path.
    if args.compare and given == 1:
        parser.error(f"--compare needs at least {MIN_COMPARE} workloads, got 1")
    return args


class Interrupted(Exception):
    """The user pressed Ctrl-C or closed stdin at a prompt."""


def read_line(prompt: str) -> str:
    """Read one line from the terminal, raising Interrupted if the user stops."""
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        # The original exception is noise here: Ctrl-C at a prompt is a decision,
        # not a fault, and callers only need to know the user stopped.
        raise Interrupted from None


def prompt_for(console: Console, label: str) -> str:
    """Read a non-empty workload description from the terminal."""
    console.print(f"[bold]{label}[/bold]")
    try:
        text = read_line(">> ")
    except Interrupted:
        console.print("\nExiting.")
        sys.exit(0)

    if not text:
        console.print("[red]Error:[/red] input cannot be empty.")
        sys.exit(1)
    return text


def collect_workloads(console: Console, args: argparse.Namespace) -> list[str]:
    """Fill in any workload descriptions not supplied on the command line."""
    workloads = list(args.workload or [])
    if args.compare:
        # However many were given on the command line, and at least two: a
        # comparison of one is not one.
        wanted = max(MIN_COMPARE, len(workloads))
        labels = [f"Workload {ORDINALS[index]}:" for index in range(wanted)]
    else:
        labels = ["Describe your workload or business problem below."]
    while len(workloads) < len(labels):
        workloads.append(prompt_for(console, labels[len(workloads)]))
    return workloads


def render_pane(markdown: str, width: int) -> list[str]:
    """Render Markdown to plain lines at a fixed width.

    Rendering each column on its own console avoids rich clipping the nested
    Markdown tables when they are placed inside a two-column layout.
    """
    buffer = io.StringIO()
    pane = Console(file=buffer, width=width, no_color=True, highlight=False)
    pane.print(Markdown(markdown))
    return buffer.getvalue().splitlines()


def pad(line: str, width: int) -> str:
    """Pad to a display width, accounting for wide characters such as emoji."""
    return line + " " * max(0, width - cell_len(line))


def spent(usage: dict, kind: str) -> dict:
    """Write one call into the shared ledger and hand its usage back.

    The CLI and the web app spend the same money against the same API key, so
    they count it in the same place. The daily ceiling the web app enforces
    reads this table.

    A failure here must not fail the run, which is the rule server._record
    already follows: the reply is in the user's hands either way, and losing a
    ledger row is a smaller problem than losing the answer they waited for.
    """
    try:
        store.record_call(usage, kind)
    except Exception:
        log.warning("could not record what a %s call cost", kind, exc_info=True)
    return usage


def paid_call(kind: str, make_call: Callable[[], Reply]) -> Reply:
    """Make one API call and write down what it cost, whatever happens next.

    The CLI's counterpart to server._paid_call, and it exists for the same
    reason. A reply truncated at the token cap, or refused, is billed like any
    other; without this the opening recommendation and every column of a
    comparison could spend real money and leave no trace, so the daily ceiling
    the web app enforces would be counting from the wrong number.

    The follow-up loop already did this by hand. Now all three paths go through
    one place.
    """
    try:
        reply = make_call()
    except AdvisorError as e:
        if e.usage:
            spent(e.usage, kind)
        raise
    spent(reply.usage, kind)
    return reply


def as_markdown(reply: Reply) -> str:
    """A reply as Markdown, whichever shape it came back in.

    A recommendation arrives as JSON validated against the schema; the CLI
    renders it back to Markdown rather than drawing components the way the web
    app does. A follow-up is already Markdown.
    """
    return reply.recommendation.to_markdown() if reply.recommendation else reply.text


def print_estimate(console: Console, reply: Reply) -> None:
    """What the architecture costs a month, and where the figure came from (F12).

    One figure: AWS list prices where there are any, the advisor's own estimate
    for the lines there are none for, and the two halves named underneath so the
    reader knows how much of it is arithmetic. The first architecture in a region
    waits on AWS's price list files, which is why there is a spinner.
    """
    if reply.recommendation is None:
        return

    try:
        with console.status("[bold green]Pricing against the AWS Price List...[/bold green]"):
            estimate = pricing.estimate(reply.recommendation)
    except Exception as e:  # a missing price is not a failed recommendation
        console.print(f"\n[dim]Not priced: {e}[/dim]")
        return

    if not estimate["hasFigure"]:
        console.print(
            "\n[dim]Nothing in this architecture could be priced, and the advisor put no "
            "figure on it either, so there is no monthly cost to give.[/dim]"
        )
        return

    label = "Estimated monthly cost" if estimate["anyEstimated"] else "Monthly cost"
    console.print(
        f"\n[bold cyan]{label}: ${estimate['monthlyUsd']:,.2f}[/bold cyan]"
        f" [dim]({estimate['region']}, on-demand list price, before discounts)[/dim]"
    )
    for service in estimate["services"]:
        if service["priced"]:
            amount = f"${service['monthlyUsd']:,.2f}"
        elif service["estimated"]:
            amount = f"~${service['monthlyUsd']:,.2f} est"
        else:
            amount = "no figure"
        console.print(f"  [dim]{service['name']:<34}[/dim] {amount:>12}")

    if estimate["anyEstimated"]:
        console.print(
            f"[dim]${estimate['pricedUsd']:,.2f} of that is AWS list prices and "
            f"${estimate['estimatedUsd']:,.2f} is the advisor's own estimate, "
            f"for the {estimate['unpricedServices']} "
            f"{'service' if estimate['unpricedServices'] == 1 else 'services'} "
            "the price list does not cover.[/dim]"
        )

    if estimate["tierGap"] >= pricing.TIER_GAP_WORTH_SAYING:
        console.print(
            f"[dim]The advisor called this {estimate['claimedTier']};"
            f" the figures come out {estimate['tier']}, which is a band apart"
            " and worth a second look at the sizing.[/dim]"
        )


def usage_line(usage: dict) -> str:
    """One line of token accounting, for the end of a run."""
    cost = f"${usage['costUsd']:.4f}" if usage.get("priced", True) else "cost unknown"
    # A web search is billed per request, so it is counted apart from the tokens
    # and only mentioned by a run that made one.
    searches = int(usage.get("searches") or 0)
    searched = f" · {searches} web {'search' if searches == 1 else 'searches'}" if searches else ""
    return (
        f"{usage['calls']} API call{'' if usage['calls'] == 1 else 's'} · "
        f"{usage['inputTokens']:,} in / {usage['outputTokens']:,} out tokens"
        f"{searched} · {cost}"
    )


def run_compare(
    console: Console, client: Anthropic, workloads: list[str], constraints: str = ""
) -> tuple[list[MessageParam], dict]:
    """Request every architecture concurrently and render them side by side.

    Each column is submitted on its own and collected on its own, rather than
    through pool.map. map propagates the first exception it meets, which threw
    away the token cost of every other column in a four-way comparison that had
    already paid for all four; paid_call records each one as it lands.
    """

    def make_job(workload: str) -> Callable[[], Reply]:
        return lambda: advise(client, workload, "comparison", None, constraints)

    with (
        console.status("[bold green]Comparing architectures...[/bold green]"),
        ThreadPoolExecutor(max_workers=len(workloads)) as pool,
    ):
        futures = [pool.submit(paid_call, "comparison", make_job(text)) for text in workloads]
        # Gathered before anything is raised, so a failure in one column still
        # leaves the others' cost counted. The first failure is then the one
        # reported, which is what pool.map did.
        replies: list[Reply] = []
        failure: AdvisorError | None = None
        for future in futures:
            try:
                replies.append(future.result())
            except AdvisorError as e:
                failure = failure or e

    if failure is not None:
        raise failure

    usage = merge_usage(*(reply.usage for reply in replies))
    columns = len(workloads)
    labels = [f"Option {chr(ord('A') + index)}" for index in range(columns)]
    # Each divider takes three characters -- space, bar, space -- and there is
    # one between each pair of columns.
    width = max(24, (console.width - 3 * (columns - 1)) // columns)
    panes: list[list[str]] = []
    messages: list[MessageParam] = []
    for workload, reply in zip(workloads, replies, strict=True):
        panes.append(render_pane(f"> {workload}\n\n---\n\n{as_markdown(reply)}", width))
        messages.append({"role": "user", "content": workload})
        messages.append({"role": "assistant", "content": reply.text})

    heads = " │ ".join(pad(label, width) for label in labels)
    console.print(f"[bold cyan]{heads}[/bold cyan]")
    console.print("─" * (width * columns + 3 * (columns - 1)), style="dim")

    for row in zip_longest(*panes, fillvalue=""):
        console.print(" │ ".join(pad(cell, width) for cell in row), markup=False, highlight=False)

    # Four columns want roughly twice the room two do, so what counts as too
    # narrow scales with how many were asked for.
    if console.width < 70 * columns:
        console.print(
            "\n[dim]Tip: widen your terminal for a more readable side-by-side view.[/dim]"
        )

    for label, reply in zip(labels, replies, strict=True):
        console.print(f"\n[bold cyan]{label}[/bold cyan]", end="")
        print_estimate(console, reply)

    return messages, usage


def run_advise(
    console: Console, client: Anthropic, workload: str, constraints: str = ""
) -> tuple[list[MessageParam], dict]:
    """Run a single recommendation followed by an interactive follow-up loop."""
    messages: list[MessageParam] = [{"role": "user", "content": workload}]

    # The first recommendation is the long wait, and most of it is spent
    # thinking before a word of the answer exists. Claude's own summary of what
    # it is weighing up goes into the spinner rather than a static "Thinking...".
    with console.status("[bold green]Thinking...[/bold green]") as status:

        def show(progress: Progress) -> None:
            lines = [line.strip() for line in progress.thinking.splitlines() if line.strip()]
            note = f" [dim]{lines[-1][:80]}[/dim]" if lines else ""
            status.update(f"[bold green]Thinking...[/bold green]{note}")

        reply = paid_call(
            "recommendation",
            lambda: call_claude(client, messages, "recommendation", show, constraints),
        )

    usage = reply.usage
    messages.append({"role": "assistant", "content": reply.text})
    console.print(Markdown(as_markdown(reply)))
    print_estimate(console, reply)

    console.print("\n[dim]Ask a follow-up question, or type 'exit' to quit.[/dim]")
    while True:
        try:
            follow_up = read_line("\n>> ")
        except Interrupted:
            console.print("\nExiting.")
            break
        if not follow_up or follow_up.lower() in EXIT_WORDS:
            break

        messages.append({"role": "user", "content": follow_up})
        try:
            with console.status("[bold green]Thinking...[/bold green]"):
                # A truncated or refused reply is still billed, and paid_call is
                # what records it either way.
                reply = paid_call("follow_up", lambda: call_claude(client, messages, "follow_up"))
        except AdvisorError as e:
            console.print(f"[red]Error:[/red] {e}")
            usage = merge_usage(usage, e.usage)
            messages.pop()
            continue

        usage = merge_usage(usage, reply.usage)
        messages.append({"role": "assistant", "content": reply.text})
        console.print(Markdown(as_markdown(reply)))

    return messages, usage


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    console = Console()

    console.print(f"[bold cyan]{branding.BRAND_NAME} {branding.PRODUCT_NAME}[/bold cyan]\n")

    constraints = constraints_line(args.region or "", args.compliance)

    try:
        client = get_client()
        workloads = collect_workloads(console, args)
        messages, usage = (
            run_compare(console, client, workloads, constraints)
            if args.compare
            else run_advise(console, client, workloads[0], constraints)
        )
    except AdvisorError as e:
        console.print(f"[red]Error:[/red] {e}")
        if e.usage:
            console.print(f"[dim]{usage_line(e.usage)}[/dim]")
        return 1

    if not args.no_save and messages:
        conversation_id = store.save(
            messages,
            mode="compare" if args.compare else "advise",
            usage=usage,
            compliance=args.compliance,
        )
        console.print(f"\n[dim]Saved as conversation {conversation_id} in {store.DB_PATH}[/dim]")

    if usage.get("calls"):
        console.print(f"[dim]{usage_line(usage)}[/dim]")

    return 0


if __name__ == "__main__":
    sys.exit(main())
