# UPGRADE.md
## Feature ideas

Ordered by what looks like value per unit of work, given what the codebase already holds.

### F14. A spend view over the ledger **[done]**

The `usage` table records every call with a model, a kind, a UK day and a cost, and the only
thing that reads it is `spent_on()` for today's ceiling. That is a month of billing history
nobody can see. A single page, or even a `python -m store --report` style command, answering
"what did August cost, split by recommendation, comparison, revision and follow-up" is close to
free: one `GROUP BY day, kind` query and a table. It also makes the daily ceiling legible, which
currently only announces itself by refusing a request.

Shipped as `store.spend_report()`, one `GROUP BY day, kind` from which the per-kind figures and
the month's total are folded rather than queried again. Two surfaces over it: `python store.py
--report [--month YYYY-MM]` and a **What this has cost** dialog on `/api/usage`, both naming the
ceiling as well as the spend. Deliberately not split by model: no caller passes `model` to
`record_call`, so every row says `MODEL` whatever served the call, and that column cannot be
trusted until a caller starts filling it in.

### F15. Diff two revisions of an architecture

A thread can hold several architectures, `latestArchitecture` already finds the newest, and every
older one is kept in full. What a reader cannot do is see what a revision changed. Given two
`Recommendation` objects the diff is mechanical: services added and removed, sizes changed,
region changed, the monthly figure before and after, notes that moved from `review` to `good`.
This is the single most defensible thing to put in front of a client after they have pushed back
on an assumption, and it is the natural companion to F10's assumption editing. It would also give
the staleness marker something better to say than a count of turns.

### F16. Price the same architecture in more than one region

`pricing.estimate` takes its region off the recommendation, and the store caches per region.
Pricing one architecture against three regions is a loop over `store.prices_for`, with no extra
model call and no new AWS files beyond the first warm of each region. "London, Ireland,
Frankfurt: $2,180 / $1,890 / $1,940 a month" answers a question clients actually ask, and it
would make the compliance selector's UK-residency constraint cost something visible.

### F17. Savings Plan and Reserved Instance context

Every figure in the product is on-demand list price, which the documents are scrupulous about
saying. The next question a client asks is what a commitment would save. The bulk Price List
carries `TermType` values other than `OnDemand`, and `extract()` already filters them out at
`pricing.py:280`. Reading the one-year and three-year no-upfront rates for the same instance keys
would let a report add "or about $1,400 a month on a one-year commitment", which is the sentence
that moves a business case. It is a genuine scope increase, hence its placement, but the plumbing
is mostly there.

### F18. Right-sizing challenges on the sizing itself

`tier_gap` already differences the model's own band against the arithmetic and says so when they
disagree by a full band. The same idea generalises: flag a Multi-AZ database under a workload the
assumptions describe as internal and low-traffic, an `m6i.24xlarge` behind "about 2,000 orders a
day", a read replica with nothing reading from it. These are heuristics over the structured
recommendation, no model call needed, and they turn the assumptions panel from a record into a
check.

### F19. Static analysis over the generated Terraform

`tests/test_terraform.py` already runs `terraform fmt -check` and, under `-m network`, `init` and
`validate`. Adding `tflint` and `checkov` to that job would catch the class of thing a consultant
will be asked about in review: a security group open to the world, an unencrypted volume, a
missing log retention. The module's README already leads with "narrow the ingress" as the first
thing to fix, which suggests the tools would agree.

### F20. Authentication, if this is ever shared

Noted rather than recommended, because the codebase is clear that this is a single-user tool and
that the spend guards "bound accidents, not attackers". If it is ever put in front of a second
person, the missing piece is not rate limiting but identity: the ledger cannot attribute spend,
the daily ceiling is shared by everyone, and `/api/conversations/<id>` will hand any conversation
to any caller. A single shared password behind `flask-login`, plus a `user` column on
`conversations` and `usage`, is the smallest version of this that would hold.

### F21. FTS5 search, when the store is big enough to want it

`store.search` documents its choice of `LIKE` over FTS5 and the reasoning is right for now: an
empty index would turn the backfill into a release step, and the scan is not what anyone waits
for. Worth revisiting once the store passes a few thousand conversations, and `migrate.py
--reindex` is already the hook that would populate it.

---
