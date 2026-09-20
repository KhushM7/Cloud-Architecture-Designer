"""Golden replies, shaped like the advisor's real output.

One recommendation and one follow-up, shared by the advisor, server, CLI and
browser suites so they all exercise the same content. Anything narrower than
these belongs inline in the test that needs it.

A recommendation now comes back as JSON validated against schema.Recommendation,
so RECOMMENDATION is the source of truth and FULL_JSON is what a call returns.
LEGACY_REPLY is the Markdown the same recommendation used to arrive as, kept for
the migration and for conversations saved before the change.
"""

import json


def usage(meter, size="", variant="", quantity=1.0, hours=730.0, estimated=0.0):
    """One line of what a service will be charged for.

    `estimated` is the model's own monthly figure for the line. It only reaches
    the total where there is no list price to find, so most lines here leave it
    at zero and the one unpriced service carries a real one -- that is what makes
    the default fixture exercise the blended path rather than the pure one.
    """
    return {
        "meter": meter,
        "size": size,
        "variant": variant,
        "quantity": quantity,
        "monthly_hours": hours,
        "estimated_monthly_usd": estimated,
    }


# What the sample architecture costs a month at London list prices, so a test
# can assert on a number without going anywhere near AWS.
PRICES = {
    ("s3-storage", ""): 0.024,
    ("application-load-balancer", ""): 0.02646,
    ("ec2-instance", "m6i.large"): 0.111,
    ("ebs-storage", ""): 0.0928,
    ("elasticache-node", "cache.m6g.large|Redis"): 0.169,
    ("rds-instance-multi-az", "db.m6g.large|MySQL"): 0.352,
    ("rds-instance", "db.m6g.large|MySQL"): 0.176,
    # What the revision below moves to, so a revised architecture prices too.
    ("ec2-instance", "m7g.large"): 0.0898,
    ("rds-instance-multi-az", "db.r6g.xlarge|PostgreSQL"): 0.742,
}

# The recommendation the tests build everything else out of.
RECOMMENDATION = {
    "headline": "Spread the load, cache the reads, scale back down after the sale",
    "overview": (
        "The current single-server setup is a reliability risk. The recommended "
        "architecture spreads the load across multiple servers, adds a caching layer, "
        "and puts a **CDN** in front."
    ),
    "region": "eu-west-2",
    # Each one is a figure the reader can disagree with, which is the whole point
    # of F10: "moderate traffic" cannot be corrected, and "2,000 orders a day"
    # can. The first two are what the sizing below actually rests on -- the
    # Auto Scaling group and the read replica both follow from them.
    "assumptions": [
        "About 2,000 orders a day, four times that on a Monday morning.",
        "Around 40 GB of order history, growing by 5 GB a month.",
        "Busy from 8am to 8pm, with almost nothing overnight.",
    ],
    "services": [
        {
            "name": "CloudFront",
            "purpose": "Content delivery network",
            "reasoning": "Serves images, CSS and JS from edge locations.",
            # Nothing in the price list this tool reads covers CloudFront, so the
            # advisor's own figure is what this line carries (F12).
            "usage": [usage("unpriced", quantity=0, hours=0, estimated=42.5)],
        },
        {
            "name": "S3",
            "purpose": "Static file storage",
            "reasoning": "Stores all static assets; pairs with CloudFront.",
            "usage": [usage("s3-storage", quantity=500, hours=0)],
        },
        {
            "name": "Application Load Balancer",
            "purpose": "Traffic distribution",
            "reasoning": "Splits requests across app servers.",
            "usage": [usage("application-load-balancer")],
        },
        {
            "name": "EC2 + Auto Scaling",
            "purpose": "Application servers",
            "reasoning": "Adds servers as traffic climbs.",
            # An instance and the disk under it are two lines on the bill.
            "usage": [
                usage("ec2-instance", size="m6i.large", quantity=3),
                usage("ebs-storage", quantity=300, hours=0),
            ],
        },
        {
            "name": "ElastiCache (Redis)",
            "purpose": "In-memory cache",
            "reasoning": "Holds common query results in memory.",
            "usage": [usage("elasticache-node", size="cache.m6g.large", variant="Redis")],
        },
        {
            "name": "RDS MySQL (Multi-AZ)",
            "purpose": "Primary database",
            "reasoning": "Adds a standby in a second data centre.",
            "usage": [
                usage("rds-instance-multi-az", size="db.m6g.large", variant="MySQL"),
            ],
        },
        {
            "name": "RDS Read Replica",
            "purpose": "Read traffic offload",
            "reasoning": "Read-only copy for product listings.",
            "usage": [usage("rds-instance", size="db.m6g.large", variant="MySQL")],
        },
    ],
    "notes": [
        {
            "pillar": "Reliability",
            "status": "good",
            "text": "Multi-AZ RDS removes the database as a single point of failure.",
        },
        {
            "pillar": "Reliability",
            "status": "good",
            "text": "Auto Scaling means no manual intervention during the spike.",
        },
        {
            "pillar": "Performance",
            "status": "good",
            "text": "ElastiCache should cut database load significantly.",
        },
        {
            "pillar": "Security",
            "status": "review",
            "text": "The load balancer should terminate HTTPS — confirm the certificate.",
        },
        {
            "pillar": "Cost",
            "status": "review",
            "text": "Auto Scaling reduces cost after peak — review the read replica.",
        },
    ],
    "cost": {
        "tier": "Medium",
        "detail": (
            "$1,800–3,600/month during peak, dropping to $500–900/month in normal operation."
        ),
    },
    # Aimed at this architecture rather than at architectures in general, which is
    # the whole point of F13: the read replica and the certificate are both things
    # the notes above flag, and neither is a question a non-engineer would think of.
    "next_questions": [
        "Do we still need the replica after peak?",
        "What happens if that data centre fails?",
        "Who renews the HTTPS certificate?",
    ],
    "diagram": """flowchart LR
  users["Users"] -->|https| cf["CloudFront"]
  cf --> s3["S3 static assets"]
  subgraph vpc["Production VPC"]
    subgraph azA["eu-west-2a"]
      alb["Application Load Balancer"] --> asg["EC2 Auto Scaling group"]
      asg --> cache["ElastiCache Redis"]
    end
    subgraph azB["eu-west-2b"]
      rds["RDS MySQL Multi AZ"] --> replica["RDS read replica"]
    end
  end
  cf --> alb
  cache -->|sql| rds
""",
}

# What a recommendation call actually returns, and what is stored on disk.
FULL_JSON = json.dumps(RECOMMENDATION, indent=2, ensure_ascii=False)

# The same workload after the conversation pinned it down: Graviton instead of
# Intel, two servers instead of three, Postgres instead of MySQL, and the read
# replica dropped. This is what a `revision` turn comes back with, and what a
# deliverable exported afterwards has to describe -- an opening brief is vague,
# and these are the numbers that make a Terraform module worth having.
REVISED = {
    **RECOMMENDATION,
    "headline": "Two Graviton servers, Postgres, and no read replica",
    "overview": (
        "With the traffic figures you gave, two Graviton instances carry the peak "
        "and the read replica earns nothing. Postgres replaces MySQL as agreed."
    ),
    "services": [
        {
            "name": "CloudFront",
            "purpose": "Content delivery network",
            "reasoning": "Serves images, CSS and JS from edge locations.",
            "usage": [usage("unpriced", quantity=0, hours=0, estimated=42.5)],
        },
        {
            "name": "S3",
            "purpose": "Static file storage",
            "reasoning": "Stores all static assets; pairs with CloudFront.",
            "usage": [usage("s3-storage", quantity=500, hours=0)],
        },
        {
            "name": "Application Load Balancer",
            "purpose": "Traffic distribution",
            "reasoning": "Splits requests across app servers.",
            "usage": [usage("application-load-balancer")],
        },
        {
            "name": "EC2 + Auto Scaling",
            "purpose": "Application servers",
            "reasoning": "Two Graviton instances at the peak you measured.",
            "usage": [
                usage("ec2-instance", size="m7g.large", quantity=2),
                usage("ebs-storage", quantity=200, hours=0),
            ],
        },
        {
            "name": "ElastiCache (Redis)",
            "purpose": "In-memory cache",
            "reasoning": "Holds common query results in memory.",
            "usage": [usage("elasticache-node", size="cache.m6g.large", variant="Redis")],
        },
        {
            "name": "RDS PostgreSQL (Multi-AZ)",
            "purpose": "Primary database",
            "reasoning": "Postgres on Graviton, sized for the write rate you gave.",
            "usage": [
                usage("rds-instance-multi-az", size="db.r6g.xlarge", variant="PostgreSQL"),
            ],
        },
    ],
    "cost": {
        "tier": "Medium",
        "detail": "$1,300\u20132,600/month at peak, $450\u2013800/month otherwise.",
    },
    "diagram": """flowchart LR
  users["Users"] --> cf["CloudFront"]
  cf --> s3["S3 static assets"]
  cf --> alb["Application Load Balancer"]
  alb --> asg["EC2 Auto Scaling group"]
  asg --> cache["ElastiCache Redis"]
  cache --> rds["RDS PostgreSQL Multi AZ"]
""",
}

REVISED_JSON = json.dumps(REVISED, indent=2, ensure_ascii=False)

# A follow-up: prose, a list, inline emphasis and code, and no structure at all.
FOLLOW_UP = """Yes — for most of the year. The replica earns its keep during the sale.

A pragmatic approach: create the replica a week before the sale, then delete it. That keeps
roughly **$220–320/month** off the bill without changing the application.

- Move reporting queries to the replica while it exists.
- Set a CloudWatch alarm on `ReplicaLag` first.
"""

# The same recommendation as it arrived before structured output: a fixed
# Markdown layout, with the awkward details migrate.py has to cope with -- a
# straddled cost tier, two pillar spellings, an unticked warning, and a diagram
# that branches and re-converges.
LEGACY_REPLY = """## AWS Architecture Recommendation

### Headline
Spread the load, cache the reads, scale back down after the sale

### Overview
The current single-server setup is a reliability risk. The recommended architecture spreads
the load across multiple servers, adds a caching layer, and puts a **CDN** in front.

### Recommended Services

| Service | Purpose | Reasoning |
|---------|---------|-----------|
| CloudFront | Content delivery network | Serves images, CSS and JS from edge locations. |
| S3 | Static file storage | Stores all static assets; pairs with CloudFront. |
| Application Load Balancer | Traffic distribution | Splits requests across app servers. |
| EC2 + Auto Scaling | Application servers | Adds servers as traffic climbs. |
| ElastiCache (Redis) | In-memory cache | Holds common query results in memory. |
| RDS MySQL (Multi-AZ) | Primary database | Adds a standby in a second data centre. |
| RDS Read Replica | Read traffic offload | Read-only copy for product listings. |

### Well-Architected Notes

- ✅ **Reliability:** Multi-AZ RDS removes the database as a single point of failure.
- ✅ **Reliability** - Auto Scaling means no manual intervention during the spike.
- ✅ **Performance**: ElastiCache should cut database load significantly.
- ⚠️ **Security**: The load balancer should terminate HTTPS — confirm the certificate.
- ⚠ Cost: Auto Scaling reduces cost after peak — review the read replica.

### Cost Tier

**Medium** — £1,500–3,000/month during peak, dropping to £400–700/month in normal operation.

### Architecture Diagram

```mermaid
flowchart LR
  users["Users"] --> cf["CloudFront"]
  cf --> s3["S3 static assets"]
  cf --> alb["Application Load Balancer"]
  alb --> asg["EC2 Auto Scaling group"]
  asg --> cache["ElastiCache Redis"]
  cache --> rds["RDS MySQL Multi AZ"]
  rds --> replica["RDS read replica"]
```
"""

# What the tests should find, so the expectations live next to the content
# rather than being spelled out again in each one.
SERVICE_COUNT = len(RECOMMENDATION["services"])
# The chips a reply offers (F13), for the tests that assert on them.
NEXT_QUESTIONS = list(RECOMMENDATION["next_questions"])
# What the sizing rests on (F10), editable on screen and a section in a report.
ASSUMPTIONS = list(RECOMMENDATION["assumptions"])
# Every service but CloudFront, which nothing here can price.
PRICED_SERVICE_COUNT = SERVICE_COUNT - 1
NOTE_COUNT = len(RECOMMENDATION["notes"])
NODE_COUNT = 8
EDGE_COUNT = 7
# The two arrows the sample names (F7). The rest carry no label, which is the
# normal case: a label is drawn where it earns its place, not on every arrow.
EDGE_LABEL_COUNT = 2
# The VPC and the two availability zones it holds (F7). Every one of them is a
# rect in the finished SVG, on top of one per node.
GROUP_COUNT = 3
