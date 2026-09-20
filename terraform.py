"""Terraform scaffolding for a reviewed architecture (F4).

The advisor already says what to build and how big each piece is: `schema.Usage`
names the meter, the instance type and the engine, because that is what the AWS
Price List has to be asked. The same lines are enough to write the Terraform, so
this module turns a recommendation into a module a consultant can open in an
editor rather than a page they have to retype.

    providers.tf            terraform block, the AWS provider, the shared tags
    variables.tf            every knob, with a default that runs
    network.tf              VPC, subnets, gateways, route tables
    security.tf             one security group a tier, rules between them
    main.tf                 the architecture itself
    outputs.tf              what the next stack needs to know
    terraform.tfvars.example
    README.md               what this is, what it is not, and what to fix first

What it is not
--------------
It is not a deployment. It is a starting point, and every file says so: the
README leads with it, `main.tf` carries it in a header comment, and the UI
labels the format the same way. Wrong infrastructure-as-code is worse than
none, so the rule here is that nothing is invented. A meter this module cannot
map to a resource is listed as unmapped in the README with the service's own
reasoning beside it, rather than guessed at.

Two things follow from that rule. The parts of an architecture the model was
never asked about -- DNS, certificates, CDN behaviours, application code, CI --
are absent rather than sketched. And the numbers that are present are the
model's own sizes: `m6i.large` in the plan is `m6i.large` here, so the estimate
on screen and the module on disk describe the same infrastructure.

What is decided here rather than by the model
---------------------------------------------
The scaffolding around the resources, where there is a defensible default and
the recommendation is silent. Three private subnets a tier across two
availability zones; IMDSv2 required; Session Manager instead of SSH keys and a
bastion; encryption at rest everywhere it is a flag; an RDS master password
managed by Secrets Manager so it is never in the state file. Each of those is a
comment where it is not obvious.

Correctness
-----------
`tests/test_terraform.py` runs `terraform fmt -check` and, where the network
allows, `terraform init -backend=false` and `terraform validate` over the
generated module for several architectures. Validation is what checks every
attribute against the provider's own schema, so this file cannot drift from what
the AWS provider actually accepts without a test going red.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date
from typing import NamedTuple

import branding
from schema import MONTHLY_HOURS, Meter, Note, Recommendation, Status, Usage

# The provider majors this is written against. Pinned to a major rather than an
# exact version: a scaffold that cannot take a patch release is not a scaffold.
TERRAFORM_VERSION = ">= 1.9"
AWS_PROVIDER = "~> 6.0"
ARCHIVE_PROVIDER = "~> 2.7"

# Amazon Linux 2023, whichever architecture the chosen instance type needs.
AMI_PATTERNS = {"x86_64": "al2023-ami-2023.*-x86_64", "arm64": "al2023-ami-2023.*-arm64"}

# An ALB target group name is allowed 32 characters and is the longest thing
# built here: "${name_prefix}-${slug}-tg" has to fit inside it, so the two
# limits are chosen together and 12 + 1 + 14 + 3 comes to 30.
SLUG_LIMIT = 14
PREFIX_LIMIT = 12


class File(NamedTuple):
    """One generated file: a path relative to the module root, and its text."""

    name: str
    text: str


# --------------------------------------------------------------------------- #
# Engines
#
# The schema's `variant` is written for a human and for the price list -- it is
# "PostgreSQL", not "postgres". These are the same engines in the names the AWS
# provider takes, with the port the tier's security group has to open.
# --------------------------------------------------------------------------- #


class Engine(NamedTuple):
    name: str
    port: int
    # SQL Server takes no initial database name and Oracle's is the SID, so only
    # the engines where `db_name` means what it says get one.
    initial_database: bool = False


RDS_ENGINES: dict[str, Engine] = {
    "postgresql": Engine("postgres", 5432, True),
    "postgres": Engine("postgres", 5432, True),
    "mysql": Engine("mysql", 3306, True),
    "mariadb": Engine("mariadb", 3306, True),
    "aurora postgresql": Engine("postgres", 5432, True),
    "aurora mysql": Engine("mysql", 3306, True),
    "oracle": Engine("oracle-se2", 1521),
    "sql server": Engine("sqlserver-se", 1433),
    "sqlserver": Engine("sqlserver-se", 1433),
}

DEFAULT_RDS = RDS_ENGINES["postgresql"]

CACHE_ENGINES: dict[str, Engine] = {
    "redis": Engine("redis", 6379),
    "valkey": Engine("valkey", 6379),
    "memcached": Engine("memcached", 11211),
}

DEFAULT_CACHE = CACHE_ENGINES["redis"]

# A service named like a copy of another database is one: RDS prices a read
# replica as an ordinary single-AZ instance, so the name is the only thing that
# distinguishes them. Wrong here means a second primary rather than a replica,
# which is why the README says which databases were read this way, and why the
# guess is only made where the engine matches the primary's and is one that takes
# a read replica without an edition change or another licence.
REPLICA_RE = re.compile(r"replica|read[- ]?only|standby|secondary", re.IGNORECASE)

REPLICATES = frozenset({"postgres", "mysql", "mariadb"})

# Instance families whose letters carry a `g` are Graviton, and need an arm64
# image: m6g, c7gn, t4g, r8g. The letters come after the generation digit, so
# `m6i` and `m6idn` are x86 and `m6g` is not.
GRAVITON_RE = re.compile(r"^[a-z]+\d+[a-z]*g")


def is_graviton(instance_type: str) -> bool:
    """Whether an instance or node type wants an arm64 image.

    The family is what comes before the first dot, once RDS's `db.` and
    ElastiCache's `cache.` are out of the way -- they carry a dot of their own, so
    they have to go first or every RDS size reads as the family "db".
    """
    family = str(instance_type or "").lower()
    family = family.removeprefix("db.").removeprefix("cache.").split(".")[0]
    return bool(GRAVITON_RE.match(family))


# --------------------------------------------------------------------------- #
# Names
#
# Two naming schemes, because Terraform and AWS do not agree on what a name is.
# `ident` is the label in the configuration: snake case, unique within a module.
# `slug` is what goes into an AWS resource name: hyphens, short, and starting
# with a letter, which is what an RDS identifier and an ALB name both demand.
# --------------------------------------------------------------------------- #


def ident(text: str, fallback: str = "resource") -> str:
    """A Terraform label: lower snake case, never starting with a digit."""
    cleaned = re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")
    if not cleaned:
        return fallback
    if cleaned[0].isdigit():
        cleaned = f"{fallback}_{cleaned}"
    return cleaned[:40].strip("_") or fallback


def slug(text: str, fallback: str = "app", limit: int = SLUG_LIMIT) -> str:
    """A fragment of an AWS resource name: hyphens, short, letter first.

    Shortened at a word boundary where there is one to shorten at. Cut to ten
    characters, `elasticache-redis` is `elasticach`, and a consultant reading the
    console should not have to work out which service that was.
    """
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    while cleaned and not cleaned[0].isalpha():
        cleaned = cleaned.lstrip("0123456789-")
    if len(cleaned) > limit:
        whole = cleaned[:limit].rpartition("-")[0]
        # A boundary is only worth taking if what is left still says something.
        # Below that, a hard cut reads better than one word of four.
        cleaned = whole if len(whole) >= 5 else cleaned[:limit]
    return cleaned.strip("-") or fallback


def prefix_for(text: str) -> str:
    """The fragment every AWS name in the module starts with.

    Short enough for the longest name built from it to fit, and long enough to
    pass the module's own validation: a workload called "Q" would otherwise
    generate a `name_prefix` of "q" and a plan that refuses to run.
    """
    candidate = slug(text, "advisor", PREFIX_LIMIT)
    return candidate if len(candidate) >= 2 else f"{candidate}-app"


class Names:
    """Keeps every generated name unique, in the order they were asked for.

    Two services called "RDS MySQL" and "RDS MySQL (reporting)" reduce to the
    same label, and a module with two `aws_db_instance.rds_mysql` blocks does
    not parse. The second one becomes `rds_mysql_2`.
    """

    def __init__(self) -> None:
        # Two sets, because a Terraform label and an AWS name are different
        # namespaces: an `aws_s3_bucket.s3` named `prefix-s3` is not a clash.
        self.labels: set[str] = set()
        self.names: set[str] = set()

    @staticmethod
    def _unique(candidate: str, taken: set[str], limit: int, joiner: str) -> str:
        name = candidate
        counter = 1
        while name in taken:
            counter += 1
            suffix = f"{joiner}{counter}"
            name = candidate[: limit - len(suffix)].strip(joiner) + suffix
        taken.add(name)
        return name

    def ident(self, text: str, fallback: str = "resource") -> str:
        return self._unique(ident(text, fallback), self.labels, 40, "_")

    def slug(self, text: str, fallback: str = "app") -> str:
        return self._unique(slug(text, fallback), self.names, SLUG_LIMIT, "-")


# --------------------------------------------------------------------------- #
# The plan
#
# What the recommendation asks for, reduced to the resources this module knows
# how to write. Everything the renderers need is decided here, so the HCL below
# is a template rather than a second place sizing decisions are taken.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Piece:
    """Shared by everything the plan holds: where it came from, what to call it."""

    ident: str
    slug: str
    service: str
    purpose: str = ""


@dataclass(frozen=True)
class Server(Piece):
    """An EC2 tier: a launch template and an auto scaling group."""

    instance_type: str = "t3.micro"
    count: int = 1
    root_gb: int = 20
    always_on: bool = True

    @property
    def architecture(self) -> str:
        return "arm64" if is_graviton(self.instance_type) else "x86_64"


@dataclass(frozen=True)
class Database(Piece):
    """An RDS instance, or a read replica of one."""

    engine: Engine = DEFAULT_RDS
    instance_class: str = "db.t4g.micro"
    multi_az: bool = False
    replica_of: str = ""


@dataclass(frozen=True)
class Cache(Piece):
    """An ElastiCache replication group, or a Memcached cluster."""

    engine: Engine = DEFAULT_CACHE
    node_type: str = "cache.t4g.micro"
    nodes: int = 1

    @property
    def clustered(self) -> bool:
        """Whether there is a second node to fail over to."""
        return self.nodes > 1


@dataclass(frozen=True)
class Bucket(Piece):
    """An S3 bucket, versioned and encrypted."""

    gb: float = 0.0


@dataclass(frozen=True)
class Function(Piece):
    """A Lambda function, packaged from a handler written beside the module."""

    requests: float = 0.0
    gb_seconds: float = 0.0


@dataclass(frozen=True)
class Balancer(Piece):
    """An application load balancer, its target group and its listeners."""

    targets: bool = False


@dataclass(frozen=True)
class Unmapped:
    """A service this module does not write, and what the advisor said about it."""

    service: str
    purpose: str
    reasoning: str
    reason: str


@dataclass(frozen=True)
class Plan:
    """A recommendation, as the resources and the gaps it comes to."""

    region: str
    prefix: str
    project: str
    servers: list[Server] = field(default_factory=list)
    databases: list[Database] = field(default_factory=list)
    caches: list[Cache] = field(default_factory=list)
    buckets: list[Bucket] = field(default_factory=list)
    functions: list[Function] = field(default_factory=list)
    balancers: list[Balancer] = field(default_factory=list)
    unmapped: list[Unmapped] = field(default_factory=list)
    reviews: list[Note] = field(default_factory=list)
    on: date = field(default_factory=date.today)

    @property
    def data_tier(self) -> bool:
        """Whether anything needs subnets with no route out."""
        return bool(self.databases or self.caches)

    @property
    def private_tier(self) -> bool:
        """Whether anything runs in the private subnets."""
        return bool(self.servers or self.functions)

    @property
    def architectures(self) -> list[str]:
        """Which Amazon Linux images the servers need, at most one of each."""
        return sorted({server.architecture for server in self.servers})

    @property
    def resources(self) -> int:
        """How many pieces of the architecture were written out."""
        return sum(
            len(group)
            for group in (
                self.servers,
                self.databases,
                self.caches,
                self.buckets,
                self.functions,
                self.balancers,
            )
        )

    @property
    def empty(self) -> bool:
        return self.resources == 0


# --------------------------------------------------------------------------- #
# Reading a recommendation
# --------------------------------------------------------------------------- #


def _quantity(usage: Usage, least: int = 1) -> int:
    """A count of instances or nodes, never below one and never absurd."""
    try:
        count = round(float(usage.quantity))
    except (TypeError, ValueError):
        return least
    return max(least, min(count, 20))


def _gigabytes(lines: list[Usage]) -> float:
    total = 0.0
    for line in lines:
        try:
            total += max(0.0, float(line.quantity))
        except (TypeError, ValueError):
            continue
    return total


def _root_volume(gb: float, instances: int) -> int:
    """The root disk each instance in a tier gets, from the tier's total.

    The plan prices one EBS figure for the whole tier, because that is how the
    bill reads. Terraform attaches a volume per instance, so it is divided back
    out. Rounded up to whole gigabytes, and never below the 20 GB an Amazon
    Linux image and a month of logs actually need.
    """
    if gb <= 0 or instances <= 0:
        return 20
    each = gb / instances
    return max(20, int(each) + (1 if each % 1 else 0))


def plan_for(recommendation: Recommendation, project: str = "", prefix: str = "") -> Plan:
    """Reduce a recommendation to the resources this module can write.

    `project` is what to call the workload -- the client name, or the headline --
    and `prefix` overrides the fragment every AWS resource name starts with.
    Both have defaults, because the generated module has to run as it stands.
    """
    names = Names()
    headline = recommendation.headline.strip()
    label = (project or headline or "advisor").strip()

    servers: list[Server] = []
    databases: list[Database] = []
    caches: list[Cache] = []
    buckets: list[Bucket] = []
    functions: list[Function] = []
    balancers: list[Balancer] = []
    unmapped: list[Unmapped] = []
    # An EBS line in a service with no instance of its own: held back and given
    # to the first tier that can carry it, rather than written out as a volume
    # with nothing attached to it.
    orphan_storage: list[tuple[str, float]] = []

    for service in recommendation.services:
        by_meter: dict[str, list[Usage]] = defaultdict(list)
        for line in service.usage:
            by_meter[str(line.meter)].append(line)

        made = False

        # The plan prices one EBS figure for a service however many instance
        # lines it has, so it goes to the first of them and any others get a root
        # volume of their own.
        storage = _gigabytes(by_meter[Meter.EBS_STORAGE])
        for line in by_meter[Meter.EC2_INSTANCE]:
            instances = _quantity(line)
            servers.append(
                Server(
                    ident=names.ident(service.name, "servers"),
                    slug=names.slug(service.name, "app"),
                    service=service.name,
                    purpose=service.purpose,
                    instance_type=line.size or "t3.micro",
                    count=instances,
                    root_gb=_root_volume(storage, instances),
                    always_on=float(line.monthly_hours or 0) >= MONTHLY_HOURS,
                )
            )
            storage = 0.0
            made = True

        if by_meter[Meter.EBS_STORAGE] and not by_meter[Meter.EC2_INSTANCE]:
            orphan_storage.append((service.name, _gigabytes(by_meter[Meter.EBS_STORAGE])))
            # Held back rather than unmapped: it becomes a root volume below if
            # there is a tier to put it on, and its own gap in the README if not.
            made = True

        for meter in (Meter.RDS_INSTANCE_MULTI_AZ, Meter.RDS_INSTANCE):
            for line in by_meter[meter]:
                engine = RDS_ENGINES.get(line.variant.strip().lower(), DEFAULT_RDS)
                databases.append(
                    Database(
                        ident=names.ident(service.name, "database"),
                        slug=names.slug(service.name, "db"),
                        service=service.name,
                        purpose=service.purpose,
                        engine=engine,
                        instance_class=line.size or "db.t4g.micro",
                        multi_az=meter == Meter.RDS_INSTANCE_MULTI_AZ,
                    )
                )
                made = True

        for line in by_meter[Meter.ELASTICACHE_NODE]:
            caches.append(
                Cache(
                    ident=names.ident(service.name, "cache"),
                    slug=names.slug(service.name, "cache"),
                    service=service.name,
                    purpose=service.purpose,
                    engine=CACHE_ENGINES.get(line.variant.strip().lower(), DEFAULT_CACHE),
                    node_type=line.size or "cache.t4g.micro",
                    nodes=_quantity(line),
                )
            )
            made = True

        if by_meter[Meter.S3_STORAGE]:
            buckets.append(
                Bucket(
                    ident=names.ident(service.name, "bucket"),
                    slug=names.slug(service.name, "data"),
                    service=service.name,
                    purpose=service.purpose,
                    gb=_gigabytes(by_meter[Meter.S3_STORAGE]),
                )
            )
            made = True

        if by_meter[Meter.LAMBDA_REQUESTS] or by_meter[Meter.LAMBDA_DURATION]:
            functions.append(
                Function(
                    ident=names.ident(service.name, "function"),
                    slug=names.slug(service.name, "fn"),
                    service=service.name,
                    purpose=service.purpose,
                    requests=_gigabytes(by_meter[Meter.LAMBDA_REQUESTS]),
                    gb_seconds=_gigabytes(by_meter[Meter.LAMBDA_DURATION]),
                )
            )
            made = True

        for line in by_meter[Meter.LOAD_BALANCER]:
            for number in range(_quantity(line)):
                suffix = service.name if number == 0 else f"{service.name} {number + 1}"
                balancers.append(
                    Balancer(
                        ident=names.ident(suffix, "balancer"),
                        slug=names.slug(suffix, "alb"),
                        service=service.name,
                        purpose=service.purpose,
                    )
                )
            made = True

        if not made:
            meters = sorted({str(line.meter) for line in service.usage})
            reason = (
                "the advisor priced nothing for it"
                if not meters or meters == [str(Meter.UNPRICED)]
                else f"nothing here writes {', '.join(meters)}"
            )
            unmapped.append(
                Unmapped(
                    service=service.name,
                    purpose=service.purpose,
                    reasoning=service.reasoning,
                    reason=reason,
                )
            )

    # The first tier carries any storage the plan put on a service of its own.
    if orphan_storage and servers:
        extra = sum(gb for _, gb in orphan_storage)
        first = servers[0]
        servers[0] = replace(
            first, root_gb=_root_volume(first.root_gb * first.count + extra, first.count)
        )
    elif orphan_storage:
        for name, gb in orphan_storage:
            unmapped.append(
                Unmapped(
                    service=name,
                    purpose="Block storage",
                    reasoning=f"{gb:,.0f} GB of EBS.",
                    reason="EBS is written as the root volume of an instance, and there is none",
                )
            )

    # Only now is it known whether there is a primary to replicate from.
    def source_for(replica: Database) -> str:
        """What a copy is a copy of, or "" where the guess does not hold up."""
        if replica.multi_az or replica.engine.name not in REPLICATES:
            return ""
        if not REPLICA_RE.search(replica.service):
            return ""
        for candidate in databases:
            if candidate is replica or candidate.engine.name != replica.engine.name:
                continue
            if candidate.multi_az or not REPLICA_RE.search(candidate.service):
                return candidate.ident
        return ""

    databases = [replace(db, replica_of=source_for(db)) for db in databases]

    # Only the first balancer gets the servers. A second one in the same plan is
    # written with an empty target group and said so in the README, rather than
    # having the same instances registered behind both.
    if balancers and servers:
        balancers = [replace(balancers[0], targets=True), *balancers[1:]]

    return Plan(
        region=recommendation.region.value,
        prefix=(slug(prefix, "", PREFIX_LIMIT) if prefix else "") or prefix_for(label),
        project=label[:60],
        servers=servers,
        databases=databases,
        caches=caches,
        buckets=buckets,
        functions=functions,
        balancers=balancers,
        unmapped=unmapped,
        reviews=[note for note in recommendation.notes if note.status is Status.REVIEW],
    )


# --------------------------------------------------------------------------- #
# Writing HCL
#
# `terraform fmt` aligns the `=` of consecutive assignments in a block, so the
# generated files are aligned the same way here rather than being formatted by
# a Terraform this machine may not have. tests/test_terraform.py runs
# `terraform fmt -check` where it does, so the two cannot drift.
# --------------------------------------------------------------------------- #

ASSIGNMENT = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*) *= *(\S.*)$")


def align(text: str) -> str:
    """Align the `=` of each run of assignments, the way `terraform fmt` does.

    A run is consecutive assignment lines at one indent. Anything else -- a
    blank line, a comment, the start or end of a nested block -- ends it, which
    is why no comment in this module sits between two assignments.
    """
    lines = text.split("\n")
    out = list(lines)
    run: list[int] = []

    def flush() -> None:
        if len(run) > 1:
            width = max(len(ASSIGNMENT.match(lines[i]).group(2)) for i in run)  # type: ignore[union-attr]
            for i in run:
                indent, key, value = ASSIGNMENT.match(lines[i]).groups()  # type: ignore[union-attr]
                out[i] = f"{indent}{key.ljust(width)} = {value}"
        run.clear()

    indent_of_run: str | None = None
    for index, line in enumerate(lines):
        match = ASSIGNMENT.match(line)
        if match is None:
            flush()
            indent_of_run = None
            continue
        if indent_of_run is not None and match.group(1) != indent_of_run:
            flush()
        indent_of_run = match.group(1)
        run.append(index)
    flush()
    return "\n".join(out)


def block(text: str) -> str:
    """One rendered block, aligned and with a blank line after it."""
    return align(text.strip("\n")) + "\n\n"


def header(title: str, note: str = "") -> str:
    """A banner comment introducing a file's contents."""
    line = "# " + "-" * 74 + "\n"
    body = f"{line}# {title}\n"
    if note:
        # Rstripped a line at a time: a blank line in a note is "#" and not "# ",
        # and `terraform fmt` does not take trailing space out of a comment.
        lines = (f"# {part}".rstrip() + "\n" for part in note.strip().split("\n"))
        body += "#\n" + "".join(lines)
    return body + line + "\n"


WARNING = f"""# Generated by the {branding.BRAND_NAME} {branding.PRODUCT_NAME}. A
# starting point, not a deployment: read every resource, size it against your
# own numbers, and plan before you apply. Nothing here has been run.
"""


# What has to be escaped inside a double-quoted HCL template, in the order it has
# to be done: the backslash first, or it would escape the escapes added after it.
#
# `${` and `%{` are the two that matter and the two this used to miss. They open
# an interpolation and a directive, and Terraform evaluates both inside a quoted
# string -- so a service name or a client name carrying one reached the generated
# module as live code rather than as text. A model writing "$5/month" produced a
# file that would not parse; `${file("/etc/passwd")}` in a client name produced
# one that read a file off the disk of whoever ran the plan and put it in a tag.
# Doubling the sigil is how HCL escapes them: `$${` renders as a literal `${`.
#
# The control characters are here because an HCL string is single-line. Model
# prose is meant to be one sentence and occasionally is not, and a raw newline in
# a quoted string is a parse error rather than a wrapped description.
HCL_ESCAPES = (
    ("\\", "\\\\"),
    ('"', '\\"'),
    ("${", "$${"),
    ("%{", "%%{"),
    ("\n", "\\n"),
    ("\r", "\\r"),
    ("\t", "\\t"),
)


def quoted(value: str) -> str:
    """A double-quoted HCL string, with everything that needs escaping escaped.

    Shorten the raw text before it gets here, never the escaped result. A cut
    taken after escaping can leave a trailing lone backslash, which escapes the
    closing quote and swallows the rest of the file; it can also split a `$${`
    back into the interpolation this defuses. Every caller that caps model text
    -- `plan.project` at `label[:60]`, `slug()`, `ident()` -- does it upstream on
    the raw value, which is what makes that safe.
    """
    text = str(value)
    for character, escape in HCL_ESCAPES:
        text = text.replace(character, escape)
    return f'"{text}"'


def cell(text: str) -> str:
    """One cell of a Markdown table: no newlines, and pipes escaped.

    The README is generated from the same model prose the HCL is, and a table has
    its own two hazards: a newline ends the row early and a `|` starts a column
    that was never in the header. Mirrors export._cell, which does this for the
    report's tables -- kept as its own copy rather than imported, because
    export.py imports terraform.py and not the other way round.
    """
    return " ".join(str(text or "").split()).replace("|", "\\|")


# --------------------------------------------------------------------------- #
# providers.tf
# --------------------------------------------------------------------------- #


def providers_tf(plan: Plan) -> str:
    providers = [
        f'    aws = {{\n      source  = "hashicorp/aws"\n      version = "{AWS_PROVIDER}"\n    }}'
    ]
    if plan.functions:
        providers.append(
            f'    archive = {{\n      source  = "hashicorp/archive"\n'
            f'      version = "{ARCHIVE_PROVIDER}"\n    }}'
        )

    out = WARNING + "\n"
    out += block(
        "terraform {\n"
        f'  required_version = "{TERRAFORM_VERSION}"\n\n'
        "  required_providers {\n" + "\n\n".join(providers) + "\n  }\n"
        "\n"
        "  # State is local, which is right for one person trying this out and wrong\n"
        "  # for anything else. Point it at a bucket before a second person runs it.\n"
        "  #\n"
        '  # backend "s3" {\n'
        '  #   bucket       = "example-terraform-state"\n'
        '  #   key          = "advisor/terraform.tfstate"\n'
        f"  #   region       = {quoted(plan.region)}\n"
        "  #   use_lockfile = true\n"
        "  # }\n"
        "}"
    )

    out += block(
        'provider "aws" {\n'
        "  region = var.aws_region\n\n"
        "  default_tags {\n"
        "    tags = local.tags\n"
        "  }\n"
        "}"
    )

    out += block(
        "locals {\n"
        "  # Every resource carries these through the provider's default_tags. Only\n"
        "  # per-resource Name tags are set below, so nothing is tagged twice.\n"
        "  tags = merge({\n"
        "    Project     = var.project\n"
        "    Environment = var.environment\n"
        '    ManagedBy   = "Terraform"\n'
        f'    Source      = "{branding.BRAND_NAME} {branding.PRODUCT_NAME}"\n'
        "  }, var.tags)\n"
        "}"
    )
    return out.rstrip("\n") + "\n"


# --------------------------------------------------------------------------- #
# variables.tf
# --------------------------------------------------------------------------- #


def variable(
    name: str,
    kind: str,
    description: str,
    default: str | None = None,
    condition: str = "",
    message: str = "",
) -> str:
    body = f"variable {quoted(name)} {{\n"
    body += f"  description = {quoted(description)}\n"
    body += f"  type        = {kind}\n"
    if default is not None:
        body += f"  default     = {default}\n"
    if condition:
        body += "\n  validation {\n"
        body += f"    condition     = {condition}\n"
        body += f"    error_message = {quoted(message)}\n"
        body += "  }\n"
    return body + "}"


def variables_tf(plan: Plan) -> str:
    out = header(
        "Variables",
        "Every one has a default that runs, so `terraform plan` works before a\n"
        "single value is filled in. The defaults are a starting point in the same\n"
        "sense the module is: right for a trial, to be argued with before production.",
    )

    out += block(
        variable(
            "aws_region",
            "string",
            "The region everything is created in. The advisor priced the architecture here.",
            quoted(plan.region),
        )
    )
    out += block(
        variable(
            "project",
            "string",
            "What this workload is called, on the Project tag of every resource.",
            quoted(plan.project or "Architecture review"),
        )
    )
    out += block(
        variable(
            "environment",
            "string",
            "Which environment this stack is. Part of the Environment tag, not of any name.",
            quoted("dev"),
            'contains(["dev", "test", "staging", "prod"], var.environment)',
            "environment must be one of dev, test, staging or prod.",
        )
    )
    out += block(
        variable(
            "name_prefix",
            "string",
            "The fragment every resource name starts with. Lower case, letter first.",
            quoted(plan.prefix),
            f'can(regex("^[a-z][a-z0-9-]{{1,{PREFIX_LIMIT - 1}}}$", var.name_prefix))',
            f"name_prefix must start with a letter, hold only lower-case letters, digits "
            f"and hyphens, and be at most {PREFIX_LIMIT} characters: the names built from "
            f"it have to fit inside what AWS allows.",
        )
    )
    out += block(
        variable(
            "tags",
            "map(string)",
            "Tags added to everything, alongside the ones this module already sets.",
            "{}",
        )
    )
    out += block(
        variable(
            "vpc_cidr",
            "string",
            "The address range of the VPC. Subnets are cut out of it as /24s.",
            quoted("10.0.0.0/16"),
            "can(cidrhost(var.vpc_cidr, 0))",
            "vpc_cidr must be a valid IPv4 CIDR block, for example 10.0.0.0/16.",
        )
    )
    out += block(
        variable(
            "az_count",
            "number",
            "How many availability zones to spread the subnets across.",
            "2",
            "var.az_count >= 2 && var.az_count <= 4",
            "az_count must be between 2 and 4. Two is the minimum a Multi-AZ database "
            "needs; more than four is a lot of subnets to read.",
        )
    )
    out += block(
        variable(
            "enable_nat_gateway",
            "bool",
            "Whether the private subnets get a route out. Needed for updates, "
            "Session Manager and anything that calls an API.",
            "true",
        )
    )
    out += block(
        variable(
            "single_nat_gateway",
            "bool",
            "One NAT gateway for every zone rather than one each. Cheaper, and a "
            "zone failure takes the others' outbound traffic with it.",
            "true",
        )
    )

    if plan.servers or plan.balancers:
        out += block(
            variable(
                "ingress_cidrs",
                "list(string)",
                "Who can reach the load balancer. Narrow this before it is anything real.",
                '["0.0.0.0/0"]',
                "length(var.ingress_cidrs) > 0",
                "ingress_cidrs must name at least one range, or nothing can reach the service.",
            )
        )
        out += block(
            variable(
                "app_port",
                "number",
                "The port the application listens on inside the private subnets.",
                "8080",
                "var.app_port > 0 && var.app_port < 65536",
                "app_port must be a TCP port between 1 and 65535.",
            )
        )

    if plan.balancers:
        out += block(
            variable(
                "health_check_path",
                "string",
                "The path the target group asks for to decide an instance is healthy.",
                quoted("/"),
            )
        )
        out += block(
            variable(
                "certificate_arn",
                "string",
                "An ACM certificate for HTTPS. Empty leaves the listener on port 80 only, "
                "which is not good enough for production.",
                quoted(""),
            )
        )

    if plan.databases or plan.balancers:
        out += block(
            variable(
                "deletion_protection",
                "bool",
                "Whether the load balancer and the databases refuse to be destroyed. "
                "False here so a trial can be torn down; true for anything holding real data.",
                "false",
            )
        )

    if plan.databases:
        out += block(
            variable(
                "db_username",
                "string",
                "The master user. Its password is generated and held in Secrets Manager, "
                "so it is never in this configuration or in the state file.",
                quoted("dbadmin"),
            )
        )
        out += block(
            variable(
                "db_name",
                "string",
                "The database created inside the instance, where the engine takes one.",
                quoted("appdb"),
                'can(regex("^[a-zA-Z][a-zA-Z0-9_]{0,62}$", var.db_name))',
                "db_name must start with a letter and hold only letters, digits and underscores.",
            )
        )
        out += block(
            variable(
                "db_allocated_storage",
                "number",
                "Gigabytes of gp3 storage each database starts with. The advisor sized the "
                "instances, not the disks, so this is a guess to be replaced.",
                "100",
                "var.db_allocated_storage >= 20",
                "db_allocated_storage must be at least 20 GB, which is the RDS minimum.",
            )
        )
        out += block(
            variable(
                "db_max_allocated_storage",
                "number",
                "The ceiling storage autoscaling may grow to. Equal to the allocated size "
                "turns autoscaling off.",
                "500",
            )
        )
        out += block(
            variable(
                "db_backup_retention_days",
                "number",
                "How many days of automated backups to keep. Zero turns them off.",
                "7",
                "var.db_backup_retention_days >= 0 && var.db_backup_retention_days <= 35",
                "db_backup_retention_days must be between 0 and 35.",
            )
        )
        out += block(
            variable(
                "db_skip_final_snapshot",
                "bool",
                "Whether destroying a database takes a last snapshot first.",
                "false",
            )
        )
        out += block(
            variable(
                "db_performance_insights",
                "bool",
                "Performance Insights, free for seven days of history. Not supported on "
                "every instance class, which is why it is off here.",
                "false",
            )
        )

    if plan.buckets:
        out += block(
            variable(
                "s3_force_destroy",
                "bool",
                "Whether destroying a bucket deletes the objects in it. Data loss on "
                "purpose, so it is off.",
                "false",
            )
        )

    if plan.functions:
        out += block(
            variable(
                "lambda_runtime",
                "string",
                "The runtime the generated handlers are built for.",
                quoted("python3.13"),
            )
        )
        out += block(
            variable(
                "lambda_memory_mb",
                "number",
                "Memory each function gets, which is also how much CPU it gets. The "
                "advisor priced GB-seconds rather than a size, so this is a starting point.",
                "512",
                "var.lambda_memory_mb >= 128 && var.lambda_memory_mb <= 10240",
                "lambda_memory_mb must be between 128 and 10240.",
            )
        )
        out += block(
            variable(
                "lambda_timeout_seconds",
                "number",
                "How long a function may run before it is killed.",
                "10",
                "var.lambda_timeout_seconds >= 1 && var.lambda_timeout_seconds <= 900",
                "lambda_timeout_seconds must be between 1 and 900.",
            )
        )
        out += block(
            variable(
                "log_retention_days",
                "number",
                "How long the functions' CloudWatch logs are kept. Zero keeps them forever.",
                "30",
            )
        )

    return out.rstrip("\n") + "\n"


# --------------------------------------------------------------------------- #
# network.tf
# --------------------------------------------------------------------------- #


def network_tf(plan: Plan) -> str:
    out = header(
        "Network",
        "One VPC, and up to three tiers of subnet in each availability zone: public\n"
        "for the load balancer, private for the compute, and -- where there is a\n"
        "database or a cache -- a data tier whose route table has no way out at all.\n"
        "\n"
        "The subnets are /24s cut out of var.vpc_cidr with cidrsubnet(), so changing\n"
        "the range changes every subnet with it and nothing has to be listed here.",
    )

    out += block(
        'data "aws_availability_zones" "available" {\n'
        '  state = "available"\n\n'
        "  filter {\n"
        '    name   = "opt-in-status"\n'
        '    values = ["opt-in-not-required"]\n'
        "  }\n"
        "}"
    )

    out += block(
        "locals {\n  azs = slice(data.aws_availability_zones.available.names, 0, var.az_count)\n}"
    )

    out += block(
        'resource "aws_vpc" "this" {\n'
        "  cidr_block           = var.vpc_cidr\n"
        "  enable_dns_support   = true\n"
        "  enable_dns_hostnames = true\n\n"
        "  tags = {\n"
        '    Name = "${var.name_prefix}-vpc"\n'
        "  }\n"
        "}"
    )

    out += block(
        'resource "aws_internet_gateway" "this" {\n'
        "  vpc_id = aws_vpc.this.id\n\n"
        "  tags = {\n"
        '    Name = "${var.name_prefix}-igw"\n'
        "  }\n"
        "}"
    )

    # Public: the load balancer, the NAT gateways, and nothing else.
    out += block(
        'resource "aws_subnet" "public" {\n'
        "  count = var.az_count\n\n"
        "  vpc_id                  = aws_vpc.this.id\n"
        "  availability_zone       = local.azs[count.index]\n"
        "  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index)\n"
        "  map_public_ip_on_launch = true\n\n"
        "  tags = {\n"
        '    Name = "${var.name_prefix}-public-${local.azs[count.index]}"\n'
        '    Tier = "public"\n'
        "  }\n"
        "}"
    )

    out += block(
        'resource "aws_subnet" "private" {\n'
        "  count = var.az_count\n\n"
        "  vpc_id            = aws_vpc.this.id\n"
        "  availability_zone = local.azs[count.index]\n"
        "  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + 8)\n\n"
        "  tags = {\n"
        '    Name = "${var.name_prefix}-private-${local.azs[count.index]}"\n'
        '    Tier = "private"\n'
        "  }\n"
        "}"
    )

    if plan.data_tier:
        out += block(
            "# The data tier's route table has no default route, so a database in it\n"
            "# cannot reach the internet even if a security group is left open.\n"
            'resource "aws_subnet" "data" {\n'
            "  count = var.az_count\n\n"
            "  vpc_id            = aws_vpc.this.id\n"
            "  availability_zone = local.azs[count.index]\n"
            "  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + 16)\n\n"
            "  tags = {\n"
            '    Name = "${var.name_prefix}-data-${local.azs[count.index]}"\n'
            '    Tier = "data"\n'
            "  }\n"
            "}"
        )

    out += block(
        'resource "aws_eip" "nat" {\n'
        "  count = var.enable_nat_gateway ? (var.single_nat_gateway ? 1 : var.az_count) : 0\n\n"
        '  domain = "vpc"\n\n'
        "  tags = {\n"
        '    Name = "${var.name_prefix}-nat-${count.index + 1}"\n'
        "  }\n"
        "}"
    )

    out += block(
        'resource "aws_nat_gateway" "this" {\n'
        "  count = var.enable_nat_gateway ? (var.single_nat_gateway ? 1 : var.az_count) : 0\n\n"
        "  allocation_id = aws_eip.nat[count.index].id\n"
        "  subnet_id     = aws_subnet.public[count.index].id\n\n"
        "  tags = {\n"
        '    Name = "${var.name_prefix}-nat-${count.index + 1}"\n'
        "  }\n\n"
        "  depends_on = [aws_internet_gateway.this]\n"
        "}"
    )

    out += block(
        'resource "aws_route_table" "public" {\n'
        "  vpc_id = aws_vpc.this.id\n\n"
        "  tags = {\n"
        '    Name = "${var.name_prefix}-public"\n'
        "  }\n"
        "}"
    )

    out += block(
        'resource "aws_route" "public_internet" {\n'
        "  route_table_id         = aws_route_table.public.id\n"
        '  destination_cidr_block = "0.0.0.0/0"\n'
        "  gateway_id             = aws_internet_gateway.this.id\n"
        "}"
    )

    out += block(
        'resource "aws_route_table_association" "public" {\n'
        "  count = var.az_count\n\n"
        "  subnet_id      = aws_subnet.public[count.index].id\n"
        "  route_table_id = aws_route_table.public.id\n"
        "}"
    )

    out += block(
        'resource "aws_route_table" "private" {\n'
        "  count = var.az_count\n\n"
        "  vpc_id = aws_vpc.this.id\n\n"
        "  tags = {\n"
        '    Name = "${var.name_prefix}-private-${local.azs[count.index]}"\n'
        "  }\n"
        "}"
    )

    out += block(
        'resource "aws_route" "private_nat" {\n'
        "  count = var.enable_nat_gateway ? var.az_count : 0\n\n"
        "  route_table_id         = aws_route_table.private[count.index].id\n"
        '  destination_cidr_block = "0.0.0.0/0"\n'
        "  nat_gateway_id         = "
        "aws_nat_gateway.this[var.single_nat_gateway ? 0 : count.index].id\n"
        "}"
    )

    out += block(
        'resource "aws_route_table_association" "private" {\n'
        "  count = var.az_count\n\n"
        "  subnet_id      = aws_subnet.private[count.index].id\n"
        "  route_table_id = aws_route_table.private[count.index].id\n"
        "}"
    )

    if plan.data_tier:
        out += block(
            'resource "aws_route_table" "data" {\n'
            "  vpc_id = aws_vpc.this.id\n\n"
            "  tags = {\n"
            '    Name = "${var.name_prefix}-data"\n'
            "  }\n"
            "}"
        )
        out += block(
            'resource "aws_route_table_association" "data" {\n'
            "  count = var.az_count\n\n"
            "  subnet_id      = aws_subnet.data[count.index].id\n"
            "  route_table_id = aws_route_table.data.id\n"
            "}"
        )

    out += block(
        "# What talked to what, and what was refused. Cheap, and the first thing\n"
        "# anyone asks for once something does not connect.\n"
        'resource "aws_flow_log" "vpc" {\n'
        "  vpc_id               = aws_vpc.this.id\n"
        '  traffic_type         = "ALL"\n'
        '  log_destination_type = "cloud-watch-logs"\n'
        "  log_destination      = aws_cloudwatch_log_group.flow_logs.arn\n"
        "  iam_role_arn         = aws_iam_role.flow_logs.arn\n\n"
        "  tags = {\n"
        '    Name = "${var.name_prefix}-flow-logs"\n'
        "  }\n"
        "}"
    )

    out += block(
        'resource "aws_cloudwatch_log_group" "flow_logs" {\n'
        '  name              = "/aws/vpc/${var.name_prefix}"\n'
        "  retention_in_days = 14\n"
        "}"
    )

    out += block(
        'data "aws_iam_policy_document" "flow_logs_assume" {\n'
        "  statement {\n"
        '    effect  = "Allow"\n'
        '    actions = ["sts:AssumeRole"]\n\n'
        "    principals {\n"
        '      type        = "Service"\n'
        '      identifiers = ["vpc-flow-logs.amazonaws.com"]\n'
        "    }\n"
        "  }\n"
        "}"
    )

    out += block(
        'data "aws_iam_policy_document" "flow_logs" {\n'
        "  statement {\n"
        '    effect = "Allow"\n\n'
        "    actions = [\n"
        '      "logs:CreateLogStream",\n'
        '      "logs:PutLogEvents",\n'
        '      "logs:DescribeLogGroups",\n'
        '      "logs:DescribeLogStreams",\n'
        "    ]\n\n"
        '    resources = ["${aws_cloudwatch_log_group.flow_logs.arn}:*"]\n'
        "  }\n"
        "}"
    )

    out += block(
        'resource "aws_iam_role" "flow_logs" {\n'
        '  name               = "${var.name_prefix}-flow-logs"\n'
        "  assume_role_policy = data.aws_iam_policy_document.flow_logs_assume.json\n"
        "}"
    )

    out += block(
        'resource "aws_iam_role_policy" "flow_logs" {\n'
        '  name   = "write-flow-logs"\n'
        "  role   = aws_iam_role.flow_logs.id\n"
        "  policy = data.aws_iam_policy_document.flow_logs.json\n"
        "}"
    )

    return out.rstrip("\n") + "\n"


# --------------------------------------------------------------------------- #
# security.tf
# --------------------------------------------------------------------------- #


def security_tf(plan: Plan) -> str:
    if not (plan.servers or plan.balancers or plan.databases or plan.caches or plan.functions):
        return ""

    out = header(
        "Security groups",
        "One group a tier, and the rules between them written as their own\n"
        "resources rather than as inline blocks: a rule that is its own resource\n"
        "can be read, moved and destroyed on its own.\n"
        "\n"
        "Every rule names the group it accepts traffic from, not a CIDR. The only\n"
        "addresses in this file are the ones reaching the load balancer from\n"
        "outside, which is the one place they belong.",
    )

    if plan.balancers:
        out += block(
            'resource "aws_security_group" "alb" {\n'
            '  name_prefix = "${var.name_prefix}-alb-"\n'
            '  description = "Public entry point"\n'
            "  vpc_id      = aws_vpc.this.id\n\n"
            "  tags = {\n"
            '    Name = "${var.name_prefix}-alb"\n'
            "  }\n\n"
            "  lifecycle {\n"
            "    create_before_destroy = true\n"
            "  }\n"
            "}"
        )
        out += block(
            'resource "aws_vpc_security_group_ingress_rule" "alb_http" {\n'
            "  for_each = toset(var.ingress_cidrs)\n\n"
            "  security_group_id = aws_security_group.alb.id\n"
            '  description       = "HTTP from ${each.value}"\n'
            "  cidr_ipv4         = each.value\n"
            "  from_port         = 80\n"
            "  to_port           = 80\n"
            '  ip_protocol       = "tcp"\n'
            "}"
        )
        out += block(
            'resource "aws_vpc_security_group_ingress_rule" "alb_https" {\n'
            '  for_each = var.certificate_arn == "" ? toset([]) : toset(var.ingress_cidrs)\n\n'
            "  security_group_id = aws_security_group.alb.id\n"
            '  description       = "HTTPS from ${each.value}"\n'
            "  cidr_ipv4         = each.value\n"
            "  from_port         = 443\n"
            "  to_port           = 443\n"
            '  ip_protocol       = "tcp"\n'
            "}"
        )
        out += block(
            'resource "aws_vpc_security_group_egress_rule" "alb_all" {\n'
            "  security_group_id = aws_security_group.alb.id\n"
            '  description       = "To the targets"\n'
            '  cidr_ipv4         = "0.0.0.0/0"\n'
            '  ip_protocol       = "-1"\n'
            "}"
        )

    if plan.servers:
        out += block(
            'resource "aws_security_group" "app" {\n'
            '  name_prefix = "${var.name_prefix}-app-"\n'
            '  description = "Application tier"\n'
            "  vpc_id      = aws_vpc.this.id\n\n"
            "  tags = {\n"
            '    Name = "${var.name_prefix}-app"\n'
            "  }\n\n"
            "  lifecycle {\n"
            "    create_before_destroy = true\n"
            "  }\n"
            "}"
        )
        if plan.balancers:
            out += block(
                'resource "aws_vpc_security_group_ingress_rule" "app_from_alb" {\n'
                "  security_group_id            = aws_security_group.app.id\n"
                '  description                  = "Application traffic from the load balancer"\n'
                "  referenced_security_group_id = aws_security_group.alb.id\n"
                "  from_port                    = var.app_port\n"
                "  to_port                      = var.app_port\n"
                '  ip_protocol                  = "tcp"\n'
                "}"
            )
        else:
            out += block(
                "# Nothing in this plan sends traffic to the instances, so the tier accepts\n"
                "# none. Add a rule here, from whatever fronts it, before it serves anything.\n"
                "#\n"
                '# resource "aws_vpc_security_group_ingress_rule" "app_from_somewhere" {\n'
                "#   security_group_id            = aws_security_group.app.id\n"
                '#   referenced_security_group_id = "sg-..."\n'
                "#   from_port                    = var.app_port\n"
                "#   to_port                      = var.app_port\n"
                '#   ip_protocol                  = "tcp"\n'
                "# }"
            )
        out += block(
            "# Wide on purpose: the instances fetch updates, reach Session Manager and\n"
            "# call AWS APIs. Narrow it with VPC endpoints rather than with a rule.\n"
            'resource "aws_vpc_security_group_egress_rule" "app_all" {\n'
            "  security_group_id = aws_security_group.app.id\n"
            '  description       = "Outbound to anywhere"\n'
            '  cidr_ipv4         = "0.0.0.0/0"\n'
            '  ip_protocol       = "-1"\n'
            "}"
        )

    if plan.functions:
        out += block(
            "# The functions are not attached to the VPC as written -- see main.tf --\n"
            "# so this group exists for the moment one of them needs a database.\n"
            'resource "aws_security_group" "lambda" {\n'
            '  name_prefix = "${var.name_prefix}-lambda-"\n'
            '  description = "Functions, when they run inside the VPC"\n'
            "  vpc_id      = aws_vpc.this.id\n\n"
            "  tags = {\n"
            '    Name = "${var.name_prefix}-lambda"\n'
            "  }\n\n"
            "  lifecycle {\n"
            "    create_before_destroy = true\n"
            "  }\n"
            "}"
        )
        out += block(
            'resource "aws_vpc_security_group_egress_rule" "lambda_all" {\n'
            "  security_group_id = aws_security_group.lambda.id\n"
            '  description       = "Outbound to anywhere"\n'
            '  cidr_ipv4         = "0.0.0.0/0"\n'
            '  ip_protocol       = "-1"\n'
            "}"
        )

    tiers = (("app", plan.servers), ("lambda", plan.functions))
    clients = [source for source, present in tiers if present]

    for database in plan.databases:
        out += block(
            f'resource "aws_security_group" "{database.ident}" {{\n'
            f'  name_prefix = "${{var.name_prefix}}-{database.slug}-"\n'
            f"  description = {quoted(database.service)}\n"
            "  vpc_id      = aws_vpc.this.id\n\n"
            "  tags = {\n"
            f'    Name = "${{var.name_prefix}}-{database.slug}"\n'
            "  }\n\n"
            "  lifecycle {\n"
            "    create_before_destroy = true\n"
            "  }\n"
            "}"
        )
        for source in clients:
            out += block(
                f'resource "aws_vpc_security_group_ingress_rule" '
                f'"{database.ident}_from_{source}" {{\n'
                f"  security_group_id            = aws_security_group.{database.ident}.id\n"
                f"  description                  = "
                f'"{database.engine.name} from the {source} tier"\n'
                f"  referenced_security_group_id = aws_security_group.{source}.id\n"
                f"  from_port                    = {database.engine.port}\n"
                f"  to_port                      = {database.engine.port}\n"
                '  ip_protocol                  = "tcp"\n'
                "}"
            )
        if not clients:
            out += block(
                f"# Nothing in this plan connects to {database.service}. Add an ingress rule\n"
                f"# for whatever does, on port {database.engine.port}.\n"
                f'resource "aws_vpc_security_group_egress_rule" "{database.ident}_none" {{\n'
                f"  security_group_id = aws_security_group.{database.ident}.id\n"
                '  description       = "Placeholder: the group is otherwise empty"\n'
                '  cidr_ipv4         = "127.0.0.1/32"\n'
                '  ip_protocol       = "-1"\n'
                "}"
            )

    for cache in plan.caches:
        out += block(
            f'resource "aws_security_group" "{cache.ident}" {{\n'
            f'  name_prefix = "${{var.name_prefix}}-{cache.slug}-"\n'
            f"  description = {quoted(cache.service)}\n"
            "  vpc_id      = aws_vpc.this.id\n\n"
            "  tags = {\n"
            f'    Name = "${{var.name_prefix}}-{cache.slug}"\n'
            "  }\n\n"
            "  lifecycle {\n"
            "    create_before_destroy = true\n"
            "  }\n"
            "}"
        )
        for source in clients:
            out += block(
                f'resource "aws_vpc_security_group_ingress_rule" '
                f'"{cache.ident}_from_{source}" {{\n'
                f"  security_group_id            = aws_security_group.{cache.ident}.id\n"
                f'  description                  = "{cache.engine.name} from the {source} tier"\n'
                f"  referenced_security_group_id = aws_security_group.{source}.id\n"
                f"  from_port                    = {cache.engine.port}\n"
                f"  to_port                      = {cache.engine.port}\n"
                '  ip_protocol                  = "tcp"\n'
                "}"
            )

    return out.rstrip("\n") + "\n"


# --------------------------------------------------------------------------- #
# main.tf
# --------------------------------------------------------------------------- #


def _amis(plan: Plan) -> str:
    out = ""
    for architecture in plan.architectures:
        out += block(
            f'data "aws_ami" "al2023_{architecture}" {{\n'
            "  most_recent = true\n"
            '  owners      = ["amazon"]\n\n'
            "  filter {\n"
            '    name   = "name"\n'
            f"    values = [{quoted(AMI_PATTERNS[architecture])}]\n"
            "  }\n\n"
            "  filter {\n"
            '    name   = "state"\n'
            '    values = ["available"]\n'
            "  }\n"
            "}"
        )
    return out


def _instance_profile(plan: Plan) -> str:
    """The role every instance gets: Session Manager, and nothing else.

    Session Manager rather than a key pair and a bastion. It needs no inbound
    port, no key to lose and no public address, and it logs who connected.
    """
    out = block(
        'data "aws_iam_policy_document" "ec2_assume" {\n'
        "  statement {\n"
        '    effect  = "Allow"\n'
        '    actions = ["sts:AssumeRole"]\n\n'
        "    principals {\n"
        '      type        = "Service"\n'
        '      identifiers = ["ec2.amazonaws.com"]\n'
        "    }\n"
        "  }\n"
        "}"
    )
    out += block(
        'resource "aws_iam_role" "instance" {\n'
        '  name               = "${var.name_prefix}-instance"\n'
        "  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json\n"
        "}"
    )
    out += block(
        "# Shell access without a key pair, a bastion or an inbound port, and with a\n"
        "# record of who connected. Add the application's own permissions beside it.\n"
        'resource "aws_iam_role_policy_attachment" "instance_ssm" {\n'
        "  role       = aws_iam_role.instance.name\n"
        '  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"\n'
        "}"
    )
    out += block(
        'resource "aws_iam_instance_profile" "instance" {\n'
        '  name = "${var.name_prefix}-instance"\n'
        "  role = aws_iam_role.instance.name\n"
        "}"
    )
    return out


def _balancer(balancer: Balancer, plan: Plan) -> str:
    out = block(
        f'resource "aws_lb" "{balancer.ident}" {{\n'
        f'  name                       = "${{var.name_prefix}}-{balancer.slug}"\n'
        '  load_balancer_type         = "application"\n'
        "  internal                   = false\n"
        "  subnets                    = aws_subnet.public[*].id\n"
        "  security_groups            = [aws_security_group.alb.id]\n"
        "  enable_deletion_protection = var.deletion_protection\n"
        "  drop_invalid_header_fields = true\n\n"
        "  tags = {\n"
        f'    Name = "${{var.name_prefix}}-{balancer.slug}"\n'
        "  }\n"
        "}"
    )

    out += block(
        f'resource "aws_lb_target_group" "{balancer.ident}" {{\n'
        f'  name        = "${{var.name_prefix}}-{balancer.slug}-tg"\n'
        "  port        = var.app_port\n"
        '  protocol    = "HTTP"\n'
        '  target_type = "instance"\n'
        "  vpc_id      = aws_vpc.this.id\n\n"
        "  health_check {\n"
        "    path                = var.health_check_path\n"
        '    matcher             = "200-399"\n'
        "    interval            = 30\n"
        "    timeout             = 5\n"
        "    healthy_threshold   = 2\n"
        "    unhealthy_threshold = 3\n"
        "  }\n\n"
        "  # Long enough for a request to finish, short enough that a deploy is not\n"
        "  # spent waiting on idle connections.\n"
        "  deregistration_delay = 30\n\n"
        "  lifecycle {\n"
        "    create_before_destroy = true\n"
        "  }\n"
        "}"
    )

    out += block(
        "# Plain HTTP, because there is no certificate by default. Set\n"
        "# var.certificate_arn and this should redirect to 443 instead of serving:\n"
        "# the redirect block is below, commented, so the change is one edit.\n"
        f'resource "aws_lb_listener" "{balancer.ident}_http" {{\n'
        f"  load_balancer_arn = aws_lb.{balancer.ident}.arn\n"
        "  port              = 80\n"
        '  protocol          = "HTTP"\n\n'
        "  default_action {\n"
        '    type             = "forward"\n'
        f"    target_group_arn = aws_lb_target_group.{balancer.ident}.arn\n"
        "  }\n\n"
        "  # default_action {\n"
        '  #   type = "redirect"\n'
        "  #\n"
        "  #   redirect {\n"
        '  #     port        = "443"\n'
        '  #     protocol    = "HTTPS"\n'
        '  #     status_code = "HTTP_301"\n'
        "  #   }\n"
        "  # }\n"
        "}"
    )

    out += block(
        f'resource "aws_lb_listener" "{balancer.ident}_https" {{\n'
        '  count = var.certificate_arn == "" ? 0 : 1\n\n'
        f"  load_balancer_arn = aws_lb.{balancer.ident}.arn\n"
        "  port              = 443\n"
        '  protocol          = "HTTPS"\n'
        '  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"\n'
        "  certificate_arn   = var.certificate_arn\n\n"
        "  default_action {\n"
        '    type             = "forward"\n'
        f"    target_group_arn = aws_lb_target_group.{balancer.ident}.arn\n"
        "  }\n"
        "}"
    )
    return out


def _server(server: Server, plan: Plan, target: str) -> str:
    hours = (
        ""
        if server.always_on
        else (
            "# The advisor sized this tier for part of the day rather than all of it, so\n"
            "# min_size is one and the group scales up to what it asked for. The policy\n"
            "# that does the scaling is below, on CPU, and is the first thing to tune.\n"
        )
    )

    out = block(
        f'resource "aws_launch_template" "{server.ident}" {{\n'
        f'  name_prefix   = "${{var.name_prefix}}-{server.slug}-"\n'
        f"  image_id      = data.aws_ami.al2023_{server.architecture}.id\n"
        f"  instance_type = {quoted(server.instance_type)}\n\n"
        "  vpc_security_group_ids = [aws_security_group.app.id]\n\n"
        "  iam_instance_profile {\n"
        "    arn = aws_iam_instance_profile.instance.arn\n"
        "  }\n\n"
        "  block_device_mappings {\n"
        '    device_name = "/dev/xvda"\n\n'
        "    ebs {\n"
        f"      volume_size           = {server.root_gb}\n"
        '      volume_type           = "gp3"\n'
        "      encrypted             = true\n"
        "      delete_on_termination = true\n"
        "    }\n"
        "  }\n\n"
        "  # IMDSv2 only. The old instance metadata service is how a request-forgery\n"
        "  # bug in an application becomes a set of credentials.\n"
        "  metadata_options {\n"
        '    http_tokens                 = "required"\n'
        '    http_endpoint               = "enabled"\n'
        "    http_put_response_hop_limit = 2\n"
        "  }\n\n"
        "  monitoring {\n"
        "    enabled = true\n"
        "  }\n\n"
        "  # The application is not deployed here. Whatever installs it -- user data,\n"
        "  # a golden image, a container agent -- goes in one of these two places.\n"
        "  #\n"
        '  # user_data = base64encode(file("${path.module}/user-data.sh"))\n\n'
        "  tag_specifications {\n"
        '    resource_type = "instance"\n\n'
        "    tags = {\n"
        f'      Name = "${{var.name_prefix}}-{server.slug}"\n'
        "    }\n"
        "  }\n\n"
        "  lifecycle {\n"
        "    create_before_destroy = true\n"
        "  }\n"
        "}"
    )

    minimum = 1 if not server.always_on else server.count
    maximum = max(server.count * 2, server.count + 1)
    out += block(
        hours + f'resource "aws_autoscaling_group" "{server.ident}" {{\n'
        f'  name_prefix         = "${{var.name_prefix}}-{server.slug}-"\n'
        f"  min_size            = {minimum}\n"
        f"  max_size            = {maximum}\n"
        f"  desired_capacity    = {server.count}\n"
        "  vpc_zone_identifier = aws_subnet.private[*].id\n"
        f"  health_check_type   = {quoted('ELB' if target else 'EC2')}\n"
        "  health_check_grace_period = 120\n"
        + (f"  target_group_arns   = [aws_lb_target_group.{target}.arn]\n" if target else "")
        + "\n"
        "  launch_template {\n"
        f"    id      = aws_launch_template.{server.ident}.id\n"
        f"    version = aws_launch_template.{server.ident}.latest_version\n"
        "  }\n\n"
        "  # A change to the launch template replaces the instances a few at a time\n"
        "  # rather than on the next termination, whenever that happens to be.\n"
        "  instance_refresh {\n"
        '    strategy = "Rolling"\n\n'
        "    preferences {\n"
        "      min_healthy_percentage = 50\n"
        "    }\n"
        "  }\n\n"
        '  dynamic "tag" {\n'
        "    for_each = merge(local.tags, "
        '{ Name = "${var.name_prefix}-' + server.slug + '" })\n\n'
        "    content {\n"
        "      key                 = tag.key\n"
        "      value               = tag.value\n"
        "      propagate_at_launch = true\n"
        "    }\n"
        "  }\n\n"
        "  lifecycle {\n"
        "    create_before_destroy = true\n"
        "  }\n"
        "}"
    )

    out += block(
        "# Target tracking rather than a pair of alarms: one number to argue about,\n"
        "# and the group works out the steps itself.\n"
        f'resource "aws_autoscaling_policy" "{server.ident}_cpu" {{\n'
        f'  name                   = "${{var.name_prefix}}-{server.slug}-cpu"\n'
        f"  autoscaling_group_name = aws_autoscaling_group.{server.ident}.name\n"
        '  policy_type            = "TargetTrackingScaling"\n\n'
        "  target_tracking_configuration {\n"
        "    target_value = 60\n\n"
        "    predefined_metric_specification {\n"
        '      predefined_metric_type = "ASGAverageCPUUtilization"\n'
        "    }\n"
        "  }\n"
        "}"
    )
    return out


def _database(database: Database, plan: Plan) -> str:
    out = ""
    if database.replica_of:
        out += block(
            f"# Read from the name of the service the advisor recommended, which is the\n"
            f"# only thing that distinguishes a replica from a second primary on a bill.\n"
            f'# If "{database.service}" is not a copy of another database, this is wrong.\n'
            f'resource "aws_db_instance" "{database.ident}" {{\n'
            f'  identifier          = "${{var.name_prefix}}-{database.slug}"\n'
            f"  replicate_source_db = aws_db_instance.{database.replica_of}.identifier\n"
            f"  instance_class      = {quoted(database.instance_class)}\n"
            f"  vpc_security_group_ids = [aws_security_group.{database.ident}.id]\n"
            "  skip_final_snapshot    = true\n"
            "  auto_minor_version_upgrade = true\n"
            "  copy_tags_to_snapshot      = true\n"
            "  performance_insights_enabled = var.db_performance_insights\n\n"
            "  # The engine, the storage and the subnet group all come from the source.\n\n"
            "  tags = {\n"
            f'    Name = "${{var.name_prefix}}-{database.slug}"\n'
            "  }\n"
            "}"
        )
        return out

    initial = (
        "  db_name  = var.db_name\n"
        if database.engine.initial_database
        else f"  # {database.engine.name} takes no initial database name here; create it after.\n"
    )

    out += block(
        f'resource "aws_db_instance" "{database.ident}" {{\n'
        f'  identifier     = "${{var.name_prefix}}-{database.slug}"\n'
        f"  engine         = {quoted(database.engine.name)}\n"
        f"  instance_class = {quoted(database.instance_class)}\n\n"
        "  # No engine_version, so AWS picks its current default for the engine. Pin a\n"
        "  # major version here once the application has been tested against one.\n\n"
        "  allocated_storage     = var.db_allocated_storage\n"
        "  max_allocated_storage = var.db_max_allocated_storage\n"
        '  storage_type          = "gp3"\n'
        "  storage_encrypted     = true\n\n"
        f"  multi_az = {str(database.multi_az).lower()}\n\n"
        + initial
        + "  username = var.db_username\n\n"
        "  # The password is generated by RDS and kept in Secrets Manager, rotated by\n"
        "  # AWS. Nothing in this repository or in the state file ever holds it.\n"
        "  manage_master_user_password = true\n\n"
        f"  db_subnet_group_name   = aws_db_subnet_group.this.name\n"
        f"  vpc_security_group_ids = [aws_security_group.{database.ident}.id]\n"
        "  publicly_accessible    = false\n\n"
        "  backup_retention_period = var.db_backup_retention_days\n"
        "  copy_tags_to_snapshot   = true\n"
        "  deletion_protection     = var.deletion_protection\n"
        "  skip_final_snapshot     = var.db_skip_final_snapshot\n"
        "  final_snapshot_identifier = var.db_skip_final_snapshot ? null : "
        f'"${{var.name_prefix}}-{database.slug}-final"\n\n'
        "  performance_insights_enabled = var.db_performance_insights\n"
        "  auto_minor_version_upgrade   = true\n"
        "  apply_immediately            = false\n\n"
        "  tags = {\n"
        f'    Name = "${{var.name_prefix}}-{database.slug}"\n'
        "  }\n"
        "}"
    )
    return out


def _cache(cache: Cache, plan: Plan) -> str:
    if cache.engine.name == "memcached":
        return block(
            f'resource "aws_elasticache_cluster" "{cache.ident}" {{\n'
            f'  cluster_id         = "${{var.name_prefix}}-{cache.slug}"\n'
            '  engine             = "memcached"\n'
            f"  node_type          = {quoted(cache.node_type)}\n"
            f"  num_cache_nodes    = {cache.nodes}\n"
            f"  port               = {cache.engine.port}\n"
            f"  az_mode            = {quoted('cross-az' if cache.clustered else 'single-az')}\n"
            "  subnet_group_name  = aws_elasticache_subnet_group.this.name\n"
            f"  security_group_ids = [aws_security_group.{cache.ident}.id]\n\n"
            "  tags = {\n"
            f'    Name = "${{var.name_prefix}}-{cache.slug}"\n'
            "  }\n"
            "}"
        )

    failover = str(cache.clustered).lower()
    return block(
        f'resource "aws_elasticache_replication_group" "{cache.ident}" {{\n'
        f'  replication_group_id = "${{var.name_prefix}}-{cache.slug}"\n'
        f"  description          = {quoted(cache.purpose or cache.service)}\n"
        f"  engine               = {quoted(cache.engine.name)}\n"
        f"  node_type            = {quoted(cache.node_type)}\n"
        f"  num_cache_clusters   = {cache.nodes}\n"
        f"  port                 = {cache.engine.port}\n\n"
        "  # No engine_version, so AWS picks its current default. Pin one before this\n"
        "  # is production, so a new default cannot arrive with a replacement.\n\n"
        "  subnet_group_name  = aws_elasticache_subnet_group.this.name\n"
        f"  security_group_ids = [aws_security_group.{cache.ident}.id]\n\n"
        f"  automatic_failover_enabled = {failover}\n"
        f"  multi_az_enabled           = {failover}\n\n"
        "  # TLS on the wire and encryption on disk. Transit encryption means every\n"
        "  # client has to speak TLS, which is worth knowing before it is switched on\n"
        "  # under a running application rather than a new one.\n"
        "  at_rest_encryption_enabled = true\n"
        "  transit_encryption_enabled = true\n\n"
        "  apply_immediately        = false\n"
        "  snapshot_retention_limit = 5\n\n"
        "  tags = {\n"
        f'    Name = "${{var.name_prefix}}-{cache.slug}"\n'
        "  }\n"
        "}"
    )


def _bucket(bucket: Bucket, plan: Plan) -> str:
    sized = (
        f"# The advisor sized this at around {bucket.gb:,.0f} GB, which is a number for the\n"
        f"# estimate rather than a setting: S3 charges for what is in it.\n"
        if bucket.gb
        else ""
    )
    out = block(
        sized + f'resource "aws_s3_bucket" "{bucket.ident}" {{\n'
        f"  # A prefix rather than a name: bucket names are global, so a fixed one\n"
        f"  # collides with somebody else's the first time this is run twice.\n"
        f'  bucket_prefix = "${{var.name_prefix}}-{bucket.slug}-"\n'
        "  force_destroy = var.s3_force_destroy\n\n"
        "  tags = {\n"
        f'    Name = "${{var.name_prefix}}-{bucket.slug}"\n'
        "  }\n"
        "}"
    )
    out += block(
        f'resource "aws_s3_bucket_public_access_block" "{bucket.ident}" {{\n'
        f"  bucket                  = aws_s3_bucket.{bucket.ident}.id\n"
        "  block_public_acls       = true\n"
        "  block_public_policy     = true\n"
        "  ignore_public_acls      = true\n"
        "  restrict_public_buckets = true\n"
        "}"
    )
    out += block(
        f'resource "aws_s3_bucket_ownership_controls" "{bucket.ident}" {{\n'
        f"  bucket = aws_s3_bucket.{bucket.ident}.id\n\n"
        "  rule {\n"
        '    object_ownership = "BucketOwnerEnforced"\n'
        "  }\n"
        "}"
    )
    out += block(
        f'resource "aws_s3_bucket_versioning" "{bucket.ident}" {{\n'
        f"  bucket = aws_s3_bucket.{bucket.ident}.id\n\n"
        "  versioning_configuration {\n"
        '    status = "Enabled"\n'
        "  }\n"
        "}"
    )
    out += block(
        f'resource "aws_s3_bucket_server_side_encryption_configuration" "{bucket.ident}" {{\n'
        f"  bucket = aws_s3_bucket.{bucket.ident}.id\n\n"
        "  rule {\n"
        "    apply_server_side_encryption_by_default {\n"
        '      sse_algorithm = "AES256"\n'
        "    }\n\n"
        "    bucket_key_enabled = true\n"
        "  }\n"
        "}"
    )
    out += block(
        "# Versioning keeps every old object forever unless something clears them up,\n"
        "# and an abandoned multipart upload is billed for storage nobody can see.\n"
        f'resource "aws_s3_bucket_lifecycle_configuration" "{bucket.ident}" {{\n'
        f"  bucket = aws_s3_bucket.{bucket.ident}.id\n\n"
        "  rule {\n"
        '    id     = "abort-incomplete-uploads"\n'
        '    status = "Enabled"\n\n'
        "    filter {}\n\n"
        "    abort_incomplete_multipart_upload {\n"
        "      days_after_initiation = 7\n"
        "    }\n"
        "  }\n\n"
        "  rule {\n"
        '    id     = "expire-old-versions"\n'
        '    status = "Enabled"\n\n'
        "    filter {}\n\n"
        "    noncurrent_version_expiration {\n"
        "      noncurrent_days = 90\n"
        "    }\n"
        "  }\n\n"
        f"  depends_on = [aws_s3_bucket_versioning.{bucket.ident}]\n"
        "}"
    )
    return out


def _functions(plan: Plan) -> str:
    out = block(
        'data "aws_iam_policy_document" "lambda_assume" {\n'
        "  statement {\n"
        '    effect  = "Allow"\n'
        '    actions = ["sts:AssumeRole"]\n\n'
        "    principals {\n"
        '      type        = "Service"\n'
        '      identifiers = ["lambda.amazonaws.com"]\n'
        "    }\n"
        "  }\n"
        "}"
    )

    for function in plan.functions:
        sized = ""
        if function.requests or function.gb_seconds:
            sized = (
                f"# The advisor priced this at around {function.requests:,.0f} requests and\n"
                f"# {function.gb_seconds:,.0f} GB-seconds a month. Neither is a setting: what\n"
                f"# they imply is var.lambda_memory_mb, and that is a guess until measured.\n"
            )

        out += block(
            f'resource "aws_iam_role" "{function.ident}" {{\n'
            f'  name               = "${{var.name_prefix}}-{function.slug}"\n'
            "  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json\n"
            "}"
        )
        out += block(
            f'resource "aws_iam_role_policy_attachment" "{function.ident}_logs" {{\n'
            f"  role       = aws_iam_role.{function.ident}.name\n"
            '  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"\n'
            "}"
        )
        out += block(
            "# Zipped from the handler beside this module, so the function deploys as it\n"
            "# stands and can be replaced by the real code without changing this file.\n"
            f'data "archive_file" "{function.ident}" {{\n'
            '  type        = "zip"\n'
            f'  source_dir  = "${{path.module}}/src/{function.slug}"\n'
            f'  output_path = "${{path.module}}/build/{function.slug}.zip"\n'
            "}"
        )
        out += block(
            sized + f'resource "aws_lambda_function" "{function.ident}" {{\n'
            f'  function_name    = "${{var.name_prefix}}-{function.slug}"\n'
            f"  role             = aws_iam_role.{function.ident}.arn\n"
            '  handler          = "handler.handler"\n'
            "  runtime          = var.lambda_runtime\n"
            '  architectures    = ["arm64"]\n'
            f"  filename         = data.archive_file.{function.ident}.output_path\n"
            f"  source_code_hash = data.archive_file.{function.ident}.output_base64sha256\n"
            "  memory_size      = var.lambda_memory_mb\n"
            "  timeout          = var.lambda_timeout_seconds\n\n"
            "  # Nothing invokes this function. Whatever should -- an API, a queue, an\n"
            "  # event -- is the next thing to add, along with the permission to do it.\n\n"
            "  # Uncomment to give the function a network interface in the VPC, which it\n"
            "  # needs to reach a database and which costs it a cold start.\n"
            "  #\n"
            "  # vpc_config {\n"
            "  #   subnet_ids         = aws_subnet.private[*].id\n"
            "  #   security_group_ids = [aws_security_group.lambda.id]\n"
            "  # }\n\n"
            "  tags = {\n"
            f'    Name = "${{var.name_prefix}}-{function.slug}"\n'
            "  }\n\n"
            f"  depends_on = [aws_cloudwatch_log_group.{function.ident}]\n"
            "}"
        )
        out += block(
            "# Declared rather than left to Lambda to create, so it has a retention\n"
            "# period instead of keeping every log line forever.\n"
            f'resource "aws_cloudwatch_log_group" "{function.ident}" {{\n'
            f'  name              = "/aws/lambda/${{var.name_prefix}}-{function.slug}"\n'
            "  retention_in_days = var.log_retention_days\n"
            "}"
        )
    return out


def main_tf(plan: Plan) -> str:
    out = WARNING + "\n"
    out += header(
        "The architecture",
        "One block a service the advisor recommended, in the order it recommended\n"
        "them. Sizes are the model's own: an instance type here is the instance type\n"
        "the estimate was priced against, so the two describe the same thing.",
    )

    if plan.empty:
        out += (
            "# Nothing in this recommendation maps to a resource this generates. The\n"
            "# network and the variables are still worth having; the services are\n"
            "# listed in README.md, with what the advisor said about each.\n"
        )
        return out.rstrip("\n") + "\n"

    if plan.unmapped:
        out += "# Recommended, and not written here. README.md says why.\n"
        for gap in plan.unmapped:
            out += f"#   {gap.service} -- {gap.reason}\n"
        out += "\n"

    if plan.servers:
        out += _amis(plan)
        out += _instance_profile(plan)

    target = plan.balancers[0].ident if plan.balancers and plan.balancers[0].targets else ""
    for balancer in plan.balancers:
        out += _balancer(balancer, plan)
        if not balancer.targets and balancer is not plan.balancers[0]:
            out += (
                f"# Nothing is registered behind {balancer.service}: the instances in this\n"
                f"# plan are already behind the first load balancer. Register what belongs\n"
                f"# here, or delete it.\n\n"
            )

    for server in plan.servers:
        out += _server(server, plan, target)

    if plan.databases:
        out += block(
            'resource "aws_db_subnet_group" "this" {\n'
            '  name       = "${var.name_prefix}-db"\n'
            "  subnet_ids = aws_subnet.data[*].id\n\n"
            "  tags = {\n"
            '    Name = "${var.name_prefix}-db"\n'
            "  }\n"
            "}"
        )
    for database in plan.databases:
        out += _database(database, plan)

    if plan.caches:
        out += block(
            'resource "aws_elasticache_subnet_group" "this" {\n'
            '  name       = "${var.name_prefix}-cache"\n'
            "  subnet_ids = aws_subnet.data[*].id\n"
            "}"
        )
    for cache in plan.caches:
        out += _cache(cache, plan)

    for bucket in plan.buckets:
        out += _bucket(bucket, plan)

    if plan.functions:
        out += _functions(plan)

    return out.rstrip("\n") + "\n"


# --------------------------------------------------------------------------- #
# outputs.tf
# --------------------------------------------------------------------------- #


def output(name: str, description: str, value: str, sensitive: bool = False) -> str:
    body = f"output {quoted(name)} {{\n"
    body += f"  description = {quoted(description)}\n"
    body += f"  value       = {value}\n"
    if sensitive:
        body += "  sensitive   = true\n"
    return body + "}"


def outputs_tf(plan: Plan) -> str:
    out = header(
        "Outputs",
        "What another stack, or a person, needs to know once this has been applied.\n"
        "No secrets: a database password is in Secrets Manager, and what comes out\n"
        "here is the name of the secret rather than what is in it.",
    )

    out += block(output("vpc_id", "The VPC everything was created in.", "aws_vpc.this.id"))
    out += block(
        output("public_subnet_ids", "The public subnets, one a zone.", "aws_subnet.public[*].id")
    )
    out += block(
        output("private_subnet_ids", "The private subnets, one a zone.", "aws_subnet.private[*].id")
    )
    if plan.data_tier:
        out += block(
            output(
                "data_subnet_ids",
                "The data subnets, which have no route out.",
                "aws_subnet.data[*].id",
            )
        )

    for balancer in plan.balancers:
        out += block(
            output(
                f"{balancer.ident}_dns_name",
                f"Where {balancer.service} answers. Point a CNAME at it.",
                f"aws_lb.{balancer.ident}.dns_name",
            )
        )

    for server in plan.servers:
        out += block(
            output(
                f"{server.ident}_asg_name",
                f"The auto scaling group behind {server.service}.",
                f"aws_autoscaling_group.{server.ident}.name",
            )
        )

    for database in plan.databases:
        out += block(
            output(
                f"{database.ident}_endpoint",
                f"Host and port for {database.service}.",
                f"aws_db_instance.{database.ident}.endpoint",
            )
        )
        if not database.replica_of:
            out += block(
                output(
                    f"{database.ident}_secret_arn",
                    f"The Secrets Manager secret holding the master password for "
                    f"{database.service}.",
                    f"try(aws_db_instance.{database.ident}.master_user_secret[0].secret_arn, null)",
                )
            )

    for cache in plan.caches:
        if cache.engine.name == "memcached":
            out += block(
                output(
                    f"{cache.ident}_endpoint",
                    f"The configuration endpoint for {cache.service}.",
                    f"aws_elasticache_cluster.{cache.ident}.configuration_endpoint",
                )
            )
        else:
            out += block(
                output(
                    f"{cache.ident}_endpoint",
                    f"The primary endpoint for {cache.service}. TLS only.",
                    f"aws_elasticache_replication_group.{cache.ident}.primary_endpoint_address",
                )
            )

    for bucket in plan.buckets:
        out += block(
            output(
                f"{bucket.ident}_bucket",
                f"The bucket created for {bucket.service}. The name is generated.",
                f"aws_s3_bucket.{bucket.ident}.bucket",
            )
        )

    for function in plan.functions:
        out += block(
            output(
                f"{function.ident}_function_name",
                f"The function created for {function.service}.",
                f"aws_lambda_function.{function.ident}.function_name",
            )
        )

    return out.rstrip("\n") + "\n"


# --------------------------------------------------------------------------- #
# terraform.tfvars.example
# --------------------------------------------------------------------------- #


def tfvars_example(plan: Plan) -> str:
    out = (
        "# Copy to terraform.tfvars and edit. Every value here is already the\n"
        "# default, so a copy with nothing changed behaves the same as no copy at all.\n"
        "#\n"
        "# terraform.tfvars is not committed: add it to .gitignore before it holds\n"
        "# anything about a real account.\n\n"
        f"aws_region  = {quoted(plan.region)}\n"
        f"project     = {quoted(plan.project or 'Architecture review')}\n"
        'environment = "dev"\n'
        f"name_prefix = {quoted(plan.prefix)}\n\n"
        'vpc_cidr = "10.0.0.0/16"\n'
        "az_count = 2\n\n"
        "# One NAT gateway is about $35 a month before traffic. Two is twice that and\n"
        "# survives a zone failing.\n"
        "enable_nat_gateway = true\n"
        "single_nat_gateway = true\n"
    )
    if plan.servers or plan.balancers:
        out += (
            "\n# Narrow this to the office, the VPN or CloudFront before it is anything real.\n"
            'ingress_cidrs = ["0.0.0.0/0"]\n'
            "app_port      = 8080\n"
        )
    if plan.balancers:
        out += (
            "\n# An ACM certificate in var.aws_region. Without one the listener is plain\n"
            "# HTTP, which is the first thing to fix.\n"
            'certificate_arn   = ""\n'
            'health_check_path = "/"\n'
        )
    if plan.databases:
        out += (
            '\ndb_username              = "dbadmin"\n'
            'db_name                  = "appdb"\n'
            "db_allocated_storage     = 100\n"
            "db_backup_retention_days = 7\n"
        )
    if plan.databases or plan.balancers:
        out += (
            "\n# True once anything here holds data worth keeping.\ndeletion_protection = false\n"
        )
    return out


# --------------------------------------------------------------------------- #
# The Lambda handlers
# --------------------------------------------------------------------------- #


def handler_py(function: Function) -> str:
    # The service name reaches a Python docstring, so it goes through cell() for
    # the newlines and then loses its quotes: a `"""` in a model-written name
    # would close the docstring and leave the rest of the file as syntax errors.
    named = cell(function.service).replace('"', "'")
    return (
        '"""Placeholder for ' + named + ".\n"
        "\n"
        "The advisor recommended this function and priced it; it did not write it.\n"
        "This exists so `terraform apply` produces something that runs, and so the\n"
        "archive the module zips is a real directory rather than a path to fill in.\n"
        "\n"
        "Replace the body. Keep the name, or change `handler` in the aws_lambda_function\n"
        "block to match.\n"
        '"""\n'
        "\n"
        "import json\n"
        "import logging\n"
        "\n"
        "log = logging.getLogger()\n"
        "log.setLevel(logging.INFO)\n"
        "\n"
        "\n"
        "def handler(event, context):\n"
        '    """Log what arrived and say so. Nothing here does any work yet."""\n'
        '    log.info("event: %s", json.dumps(event, default=str))\n'
        "    return {\n"
        '        "statusCode": 200,\n'
        '        "headers": {"content-type": "application/json"},\n'
        '        "body": json.dumps({"ok": True, "function": context.function_name}),\n'
        "    }\n"
    )


# --------------------------------------------------------------------------- #
# README.md
# --------------------------------------------------------------------------- #


def _resource_lines(plan: Plan) -> list[str]:
    rows = []
    for server in plan.servers:
        rows.append(
            f"| `aws_autoscaling_group.{server.ident}` | {cell(server.service)} | "
            f"{server.count} x {server.instance_type}, {server.root_gb} GB gp3 root, "
            f"{server.architecture} |"
        )
    for balancer in plan.balancers:
        behind = "the instances above" if balancer.targets else "nothing yet"
        rows.append(
            f"| `aws_lb.{balancer.ident}` | {cell(balancer.service)} | "
            f"internet-facing, HTTP listener, {behind} behind it |"
        )
    for database in plan.databases:
        shape = (
            f"read replica of `{database.replica_of}`"
            if database.replica_of
            else ("Multi-AZ" if database.multi_az else "single-AZ")
        )
        rows.append(
            f"| `aws_db_instance.{database.ident}` | {cell(database.service)} | "
            f"{database.engine.name}, {database.instance_class}, {shape} |"
        )
    for cache in plan.caches:
        kind = (
            "aws_elasticache_cluster"
            if cache.engine.name == "memcached"
            else "aws_elasticache_replication_group"
        )
        rows.append(
            f"| `{kind}.{cache.ident}` | {cell(cache.service)} | "
            f"{cache.engine.name}, {cache.nodes} x {cache.node_type}"
            f"{', automatic failover' if cache.clustered else ''} |"
        )
    for bucket in plan.buckets:
        sized = f", sized at ~{bucket.gb:,.0f} GB" if bucket.gb else ""
        rows.append(
            f"| `aws_s3_bucket.{bucket.ident}` | {cell(bucket.service)} | "
            f"versioned, encrypted, private{sized} |"
        )
    for function in plan.functions:
        rows.append(
            f"| `aws_lambda_function.{function.ident}` | {cell(function.service)} | "
            f"placeholder handler in `src/{function.slug}/`, nothing invokes it |"
        )
    return rows


def readme_md(plan: Plan) -> str:
    prose: list[str] = []
    prose.append("# Terraform: " + (plan.project or "reviewed architecture"))
    prose.append(
        "**This is a starting point, not a deployment.** It was generated from an "
        f"architecture review by the {branding.BRAND_NAME} {branding.PRODUCT_NAME}, "
        "and nothing in "
        "it has been applied to an account. Read every resource, size it against your "
        "own numbers, and run `terraform plan` before `terraform apply`."
    )
    prose.append(
        f"Written on {plan.on.day} {plan.on:%B %Y} for `{plan.region}`. The sizes here "
        "are the ones the review was priced against, so the estimate in the report and "
        "this module describe the same infrastructure."
    )

    prose.append("## What it creates")
    rows = _resource_lines(plan)
    if rows:
        prose.append(
            "Alongside a VPC with public, private"
            + (" and data" if plan.data_tier else "")
            + " subnets across two availability zones, a NAT gateway, route tables, "
            "VPC flow logs and a security group a tier:"
        )
        prose.append("| Resource | Recommended as | Shape |\n|---|---|---|\n" + "\n".join(rows))
    else:
        prose.append(
            "Nothing but the network. No service in this review maps to a resource this "
            "generator writes, so what it leaves you is a VPC to build in."
        )

    if plan.unmapped:
        prose.append("## What it does not create")
        prose.append(
            "These were recommended and are not here. Each is a deliberate gap rather "
            "than an oversight: wrong infrastructure-as-code is worse than none, so a "
            "service this generator cannot size is named rather than guessed at."
        )
        prose.append(
            "| Service | What it was for | Why it is missing |\n|---|---|---|\n"
            + "\n".join(
                f"| {cell(gap.service)} | {cell(gap.purpose or gap.reasoning)} "
                f"| {cell(gap.reason)} |"
                for gap in plan.unmapped
            )
        )

    prose.append("## Running it")
    prose.append(
        "```sh\n"
        "terraform init\n"
        "terraform plan     # read this properly. It is the point of the exercise.\n"
        "terraform apply\n"
        "```"
    )
    prose.append(
        "Credentials come from the environment, the same as any other Terraform: an "
        "`AWS_PROFILE`, an assumed role, or whatever your organisation uses. Nothing "
        "here reads a key from a file."
    )
    prose.append(
        "State is local. That is right for one person trying this out and wrong for "
        "anything else, so `providers.tf` carries a commented S3 backend to fill in "
        "before a second person runs it."
    )

    prose.append("## Before this goes anywhere real")
    checks = [
        "**Narrow the ingress.** `var.ingress_cidrs` is `0.0.0.0/0`. Set it to the "
        "office, the VPN or CloudFront."
        if plan.servers or plan.balancers
        else None,
        "**Terminate TLS.** Set `var.certificate_arn` to an ACM certificate and switch "
        "the port 80 listener to the redirect that is commented in beside it."
        if plan.balancers
        else None,
        "**Pin the engine versions.** No `engine_version` is set, so AWS picks its own "
        "default and a new default is a replacement waiting to happen."
        if plan.databases or plan.caches
        else None,
        "**Turn on deletion protection.** `var.deletion_protection` is `false` so a "
        "trial can be torn down."
        if plan.databases or plan.balancers
        else None,
        "**Deploy something.** The launch template installs no application: user data, "
        "a golden image or a container agent is the next decision."
        if plan.servers
        else None,
        "**Write the functions.** `src/` holds a handler that logs its event and "
        "returns 200, and nothing invokes it."
        if plan.functions
        else None,
        "**Move the state somewhere shared**, with locking.",
        "**Read the estimate again.** The report's monthly figure is a floor: it is "
        "list price, in dollars, before discounts, and it counts only what could be "
        "priced.",
    ]
    prose.append("\n".join(f"- {check}" for check in checks if check))

    if plan.reviews:
        prose.append("## What the review flagged")
        prose.append(
            "The Well-Architected notes marked for a second look, from the same review "
            "this module came from. None of them is fixed here."
        )
        prose.append(
            "\n".join(f"- **{note.pillar.value}** — {cell(note.text)}" for note in plan.reviews)
        )

    prose.append("## How this was generated")
    prose.append(
        "By `terraform.py` in the Advisor, from the recommendation's own service and "
        "usage lines. It is a template: the same review generates the same module, and "
        "editing the module is expected. Nothing reads it back."
    )

    return "\n\n".join(part.strip() for part in prose if part) + "\n"


# --------------------------------------------------------------------------- #
# The module
# --------------------------------------------------------------------------- #


def files(recommendation: Recommendation, project: str = "", prefix: str = "") -> list[File]:
    """The whole Terraform module for one recommendation, in reading order."""
    plan = plan_for(recommendation, project=project, prefix=prefix)
    written = [
        File("README.md", readme_md(plan)),
        File("providers.tf", providers_tf(plan)),
        File("variables.tf", variables_tf(plan)),
        File("network.tf", network_tf(plan)),
        File("main.tf", main_tf(plan)),
        File("outputs.tf", outputs_tf(plan)),
        File("terraform.tfvars.example", tfvars_example(plan)),
    ]
    security = security_tf(plan)
    if security:
        written.insert(4, File("security.tf", security))
    for function in plan.functions:
        written.append(File(f"src/{function.slug}/handler.py", handler_py(function)))
    return written
