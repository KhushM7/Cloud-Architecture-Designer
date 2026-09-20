"""The shape of an architecture recommendation.

These models are the contract between Claude and the rest of the app. They are
sent to the API as a JSON schema (`output_format=Recommendation`), so the reply
comes back validated against them rather than scraped out of Markdown, and the
same classes are what advisor.py, server.py and main.py pass around.

Field descriptions are part of the schema, so they are the instructions for that
field. Anything the model needs to know about *what* to write belongs here; the
system prompt in advisor.py only says who it is talking to and why.
"""

from enum import StrEnum

from pydantic import BaseModel, Field


class Pillar(StrEnum):
    """The AWS Well-Architected pillars a note can be filed under."""

    RELIABILITY = "Reliability"
    SECURITY = "Security"
    PERFORMANCE = "Performance"
    COST = "Cost"
    OPERATIONS = "Operations"
    SUSTAINABILITY = "Sustainability"


# The framework's own names for the pillars, and where each one is documented
# (F6). These are metadata, not schema: they are attached while a reply is being
# rendered and they never travel to the model. A URL in a field description is an
# invitation to write a plausible one, and a citation nobody can follow is worse
# than no citation.
#
# The short names above are what a Note is stored as, so they cannot be renamed
# to match these without rewriting every conversation in the store. Three of them
# differ from the official name, which is why migrate.PILLAR_ALIASES exists.
PILLAR_OFFICIAL: dict[Pillar, str] = {
    Pillar.RELIABILITY: "Reliability",
    Pillar.SECURITY: "Security",
    Pillar.PERFORMANCE: "Performance Efficiency",
    Pillar.COST: "Cost Optimization",
    Pillar.OPERATIONS: "Operational Excellence",
    Pillar.SUSTAINABILITY: "Sustainability",
}

_WA_DOCS = "https://docs.aws.amazon.com/wellarchitected/latest"

PILLAR_DOCS: dict[Pillar, str] = {
    Pillar.RELIABILITY: f"{_WA_DOCS}/reliability-pillar/welcome.html",
    Pillar.SECURITY: f"{_WA_DOCS}/security-pillar/welcome.html",
    Pillar.PERFORMANCE: f"{_WA_DOCS}/performance-efficiency-pillar/welcome.html",
    Pillar.COST: f"{_WA_DOCS}/cost-optimization-pillar/welcome.html",
    Pillar.OPERATIONS: f"{_WA_DOCS}/operational-excellence-pillar/welcome.html",
    Pillar.SUSTAINABILITY: f"{_WA_DOCS}/sustainability-pillar/sustainability-pillar.html",
}


class Status(StrEnum):
    """Whether a note is something handled or something to look at."""

    GOOD = "good"
    REVIEW = "review"


class Tier(StrEnum):
    """Indicative monthly cost, including the two straddled tiers."""

    LOW = "Low"
    LOW_MEDIUM = "Low–Medium"
    MEDIUM = "Medium"
    MEDIUM_HIGH = "Medium–High"
    HIGH = "High"


class Region(StrEnum):
    """The regions the advisor prices against."""

    LONDON = "eu-west-2"
    IRELAND = "eu-west-1"
    FRANKFURT = "eu-central-1"
    STOCKHOLM = "eu-north-1"
    N_VIRGINIA = "us-east-1"
    OREGON = "us-west-2"
    SINGAPORE = "ap-southeast-1"
    SYDNEY = "ap-southeast-2"


class Compliance(StrEnum):
    """A regime the architecture has to stand up to (F5).

    An input rather than an output: the user picks one, and it reaches the model
    as a constraint on the turn. NONE is the honest default -- most workloads
    have no named regime, and inventing one changes the advice for no reason.

    The values are what a stored conversation carries, so they are slugs rather
    than prose; the sentence each one becomes is in advisor.py, where the rest of
    the prompt lives.
    """

    NONE = "none"
    UK_DATA_RESIDENCY = "uk-data-residency"
    PCI_DSS = "pci-dss"
    HIPAA = "hipaa"
    NHS_DSPT = "nhs-dspt"


class Meter(StrEnum):
    """What a line of usage is charged by.

    These are the things the AWS Price List can be asked about directly, and
    between them they cover most of what a bill for one of these architectures
    is made of. Anything else is UNPRICED: named in the breakdown, counted in
    nothing, and honest about it.
    """

    EC2_INSTANCE = "ec2-instance"
    EBS_STORAGE = "ebs-storage"
    RDS_INSTANCE = "rds-instance"
    RDS_INSTANCE_MULTI_AZ = "rds-instance-multi-az"
    ELASTICACHE_NODE = "elasticache-node"
    LOAD_BALANCER = "application-load-balancer"
    S3_STORAGE = "s3-storage"
    LAMBDA_REQUESTS = "lambda-requests"
    LAMBDA_DURATION = "lambda-duration"
    UNPRICED = "unpriced"


# Hours in an average month: what "running all the time" means on a bill.
MONTHLY_HOURS = 730.0


class Usage(BaseModel):
    """One thing that will appear as a line on the bill.

    A service can have more than one: an EC2 instance and the EBS volume
    attached to it are charged separately, so they are two lines.
    """

    meter: Meter = Field(
        description="What this line is charged by. A standby in a second availability "
        "zone is charged for, so a Multi-AZ database is 'rds-instance-multi-az' rather "
        "than 'rds-instance'. Use 'unpriced' for a service that none of the others "
        "describe -- CloudFront, Route 53, a NAT gateway -- rather than forcing it into "
        "one that does not fit."
    )
    size: str = Field(
        description="The instance or node type: 'm6i.large' for EC2, 'db.r6g.xlarge' for "
        "RDS, 'cache.t4g.micro' for ElastiCache. Empty for every other meter."
    )
    variant: str = Field(
        description="The engine, where the price depends on one: 'PostgreSQL', 'MySQL', "
        "'MariaDB', 'Oracle' or 'SQL Server' for RDS; 'Redis', 'Valkey' or 'Memcached' "
        "for ElastiCache. Empty for every other meter."
    )
    quantity: float = Field(
        description="How many. Instances, nodes or load balancers for the hourly meters; "
        "gigabytes stored for the storage meters; requests a month for lambda-requests; "
        "GB-seconds a month for lambda-duration. 0 for an unpriced line."
    )
    monthly_hours: float = Field(
        description="Hours a month each one runs, for the hourly meters: 730 for "
        "something always on, less for something that scales down outside peak. 0 for "
        "the meters that are not charged by the hour."
    )
    # Last, because a price is only answerable once the sizing above it is settled,
    # and field order is fill order.
    estimated_monthly_usd: float = Field(
        description="What this line costs a month in US dollars, at on-demand list "
        "prices, as your own estimate. Give a figure for every line, including the ones "
        "you expect can be looked up: the app prices what it can against the AWS Price "
        "List and falls back to your figure only where there is no list price to find, "
        "so on the rest yours is a cross-check on your own sizing. One month of exactly "
        "the usage described above -- this quantity, these hours, this region -- not a "
        "year, not an hour, and not a range. 0 only if you genuinely cannot put a "
        "number on it."
    )


class Service(BaseModel):
    """One row of the recommended services table."""

    name: str = Field(description="The AWS service name, for example 'RDS MySQL (Multi-AZ)'.")
    purpose: str = Field(description="What the service does here, in five words or fewer.")
    reasoning: str = Field(description="One sentence on why this service, and not another.")
    usage: list[Usage] = Field(
        description="What this service will be charged for, so the architecture can be "
        "priced against the AWS Price List rather than guessed at. Give the sizes you "
        "would actually pick. One entry is usual; give two where the service is charged "
        "in two ways, such as an instance and the storage attached to it. A service you "
        "cannot size is a single 'unpriced' entry."
    )


class Note(BaseModel):
    """One Well-Architected observation."""

    pillar: Pillar
    status: Status = Field(
        description="'good' if the architecture already handles this, "
        "'review' if it is a gap or a trade-off worth a second look."
    )
    text: str = Field(description="The observation itself, in one sentence. No pillar prefix.")


class Cost(BaseModel):
    """The model's own read on the monthly cost.

    Neither of these is what the reader is shown. The figure on screen and in an
    export is the priced one from pricing.py, and the band beside it is derived
    from that figure. These two are kept because they are written before anything
    is looked up, which makes them a check on the model's own sizing: a tier two
    bands away from the arithmetic is a mis-sized architecture, and that is worth
    knowing. See pricing.tier_gap().
    """

    tier: Tier = Field(
        description="Your own judgement of the monthly cost band, before anything is "
        "looked up. The app derives the band it shows the reader from the priced total; "
        "this one is kept beside it as a check on your sizing, so answer it "
        "independently rather than trying to guess what the arithmetic will say."
    )
    detail: str = Field(
        description="A rough monthly range in US dollars, distinguishing peak from "
        "normal operation where that matters. For example: "
        "'$1,800–3,600/month at peak, $500–900/month otherwise'. US dollars because "
        "that is what AWS publishes its prices in, and this sits beside them."
    )


# The renderer in static/app.js lays this out itself rather than calling Mermaid,
# so the rules below are what keep a diagram drawable. They are enforced by
# prompt, not by schema: JSON schema cannot describe Mermaid syntax.
DIAGRAM_RULES = """A Mermaid flowchart of the architecture. Follow these rules exactly:
start with `flowchart LR`; write every node as `id["Label"]` with a short \
alphanumeric id and the label in double quotes; declare each label once and refer \
to the node by id afterwards; never put brackets, parentheses, quotes, `<` or `>` \
inside a label; keep to the main services and the flow between them, around 6 to \
12 nodes; where an arrow is worth naming, label it as `a -->|writes to| b`, three \
words at most, letters, digits and spaces only, and never a `|` inside the label; \
where the architecture has a boundary worth showing, wrap those nodes in \
`subgraph id["Label"]` and `end`, declaring each node inside the boundary it \
belongs to, with availability zones nested inside a VPC and nothing nested deeper \
than that."""


class Recommendation(BaseModel):
    """A full architecture recommendation.

    Field order is the order Claude fills them in, and the order the web app
    reveals them while the reply streams, so it runs headline first and diagram
    last -- shortest and most useful to slowest.
    """

    headline: str = Field(
        description="A short, concrete summary of the approach in the user's own terms, "
        "no more than about twelve words. No Markdown, no trailing full stop, "
        "and no AWS service names."
    )
    overview: str = Field(
        description="A short paragraph, in plain English, explaining the reasoning behind "
        "the architecture. Markdown emphasis is allowed; headings and lists are not."
    )
    region: Region = Field(
        description="Where this runs. eu-west-2 (London) unless the workload calls for "
        "somewhere else -- data residency, or users concentrated in another part of the "
        "world. Everything is priced against this region."
    )
    # Defaulted for the same reason next_questions is, below, and it is
    # backward compatibility rather than taste: /api/estimate validates a stored
    # reply against this model strictly, so a field required now would make
    # every conversation saved before today unpriceable.
    #
    # It sits above the services because field order is fill order. What the
    # sizing rests on is written before the sizing, rather than reconstructed
    # afterwards to fit it, and a reader gets it early enough to argue with (F10).
    assumptions: list[str] = Field(
        default_factory=list,
        description="Three or four things you are taking as read that the brief did not "
        "say: traffic at peak, how much data there is and how fast it grows, the hours it "
        "has to be up, how many people use it. One sentence each, with a figure in it the "
        "reader can disagree with -- 'about 2,000 orders a day, four times that on a "
        "Monday' rather than 'moderate traffic'. Your sizing rests on these, so state the "
        "ones that would change the architecture if they turned out to be wrong, and do "
        "not restate anything the brief already told you.",
    )
    services: list[Service] = Field(
        description="The AWS services this architecture is built from, most important first."
    )
    notes: list[Note] = Field(
        description="Well-Architected observations, one pillar each, covering all six "
        "pillars. Where a pillar genuinely does not bear on this workload, still file a "
        "note for it saying so in a sentence: a reader is entitled to know a pillar was "
        "considered and dismissed rather than forgotten. More than one note on a pillar "
        "is fine where there is more than one thing worth saying."
    )
    cost: Cost
    # Defaulted, like assumptions above, and for the same reason: backward
    # compatibility rather than taste. /api/estimate validates a stored reply
    # against this model strictly, and the browser calls it after every render,
    # so a required field added now would make every conversation saved before
    # today unpriceable. A default means an old reply still validates and simply
    # has no chips. It sits between cost and diagram because field order is fill
    # order: after it the chips are on screen before the diagram lands, and
    # before it they would delay the whole document.
    next_questions: list[str] = Field(
        default_factory=list,
        description="Two or three questions this particular reader would not know to ask "
        "but should, phrased as they would ask them and no more than about eight words "
        "each. Aim them at what is specific to this architecture: a single-AZ database "
        "invites asking what happens when that data centre fails, a read replica invites "
        "asking whether it is still needed outside the busy period. Do not ask anything "
        "the brief already answered, and do not suggest rebuilding or revising the "
        "architecture -- that is a button of its own and the reader has it.",
    )
    diagram: str = Field(description=DIAGRAM_RULES)

    def to_markdown(self) -> str:
        """Render back to Markdown, for the CLI and for a plain-text export.

        The web app renders each field as its own component and never calls this.
        """
        services = "\n".join(
            f"| {service.name} | {service.purpose} | {service.reasoning} |"
            for service in self.services
        )
        assumed = (
            "### What this assumes\n\n"
            + "\n".join(f"- {line}" for line in self.assumptions)
            + "\n\n"
            if self.assumptions
            else ""
        )
        notes = "\n".join(
            f"- {'✅' if note.status is Status.GOOD else '⚠️'} **{note.pillar.value}:** {note.text}"
            for note in self.notes
        )
        return (
            "## AWS Architecture Recommendation\n\n"
            f"### Headline\n{self.headline}\n\n"
            f"### Overview\n{self.overview}\n\n"
            f"Region: **{self.region.value}**\n\n"
            f"{assumed}"
            "### Recommended Services\n\n"
            "| Service | Purpose | Reasoning |\n|---|---|---|\n"
            f"{services}\n\n"
            f"### Well-Architected Notes\n\n{notes}\n\n"
            f"### Cost\n\nIndicatively **{self.cost.tier.value}** — {self.cost.detail}\n\n"
            f"### Architecture Diagram\n\n```mermaid\n{self.diagram.strip()}\n```\n"
        )
