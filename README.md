# Architecture Advisor

Describe an AWS workload in plain English and get back a structured architecture
recommendation: a services table, Well-Architected notes, and a monthly cost priced
against AWS's own published rates. Powered by the Claude API.

Two front doors, one core. A Rich terminal app and a Flask web app both call
`advisor.py`, so a conversation started in one can be reopened in the other.

## What it does

| | |
|---|---|
| **A structured recommendation** | Claude's reply is validated against a JSON schema rather than scraped out of Markdown: a headline, an overview, the assumptions behind the sizing, a services table with instance types and quantities, Well-Architected notes, a cost figure and a diagram. |
| **Real pricing** | Every sized line is looked up in AWS's own bulk Price List and totalled into one monthly figure, with a Low/Medium/High band derived from that total. No credentials needed, and no exchange rate invented. |
| **Stated assumptions** | The three or four things the sizing rests on are written before the sizing, as figures you can correct in place. Correcting them rebuilds the architecture on the corrected ones. |
| **Well-Architected notes** | Each note is filed under one of the six pillars, linked to that pillar's documentation, and counted, so a review says how much of the framework it actually spoke to. |
| **A drawn diagram** | The `flowchart LR` the model writes is laid out and drawn in the house style, with edge labels, VPC and availability-zone boundaries, and an SVG or PNG download. No build step, no runtime dependency. |
| **Region and compliance as inputs** | Pin the region the architecture runs in and the regime it has to stand up to (UK data residency, PCI DSS, HIPAA, NHS DSPT). Both change the architecture, not just a disclaimer on it. |
| **Follow-up questions** | Ask anything about the architecture in prose. A follow-up may search AWS's own documentation and cite the pages it read. The suggested chips are written from the architecture on screen. |
| **Revision on request** | A button asks for the whole architecture again, built on what the conversation settled. Nothing infers a revision from what you typed. |
| **Two to four options side by side** | The Compare tab builds up to four architectures to the same region and compliance profile, streaming into columns that share their rows. |
| **A client deliverable** | Export as a branded PDF, a self-contained web page, Markdown, the diagram as SVG and PNG, the raw session as JSON, and optionally a Terraform module. Written from one document model, so section 3 is section 3 in all of them. |
| **Terraform scaffolding** | A reviewable module built from the sizes the estimate was priced against, with what could not be sized named as a gap rather than guessed at. |
| **Searchable history** | Conversations from both front doors live in one SQLite store, searchable by text and filterable by cost band, service or a tag you added. |
| **Spend guards** | Rate limits per tab and per IP, a daily token ceiling counted across the CLI and the web app together, and a ledger with one row per API call. |
| **Streaming everywhere** | Every call streams. A recommendation renders field by field as it is written rather than sitting behind a spinner. |

## Setup

Needs Python 3.11+.

```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt   # only to run the tests, the linter or mypy
```

Then set your API key, either in a `.env` file at the project root:

```
ANTHROPIC_API_KEY="your-key-here"
```

or in your environment:

```bash
export ANTHROPIC_API_KEY="your-key-here"   # macOS/Linux
$env:ANTHROPIC_API_KEY="your-key-here"     # Windows PowerShell
```

## Command line

```bash
python main.py                    # interactive session with follow-up questions
python main.py -w "Your workload description here"
python main.py --compare          # two to four workloads, side by side
python main.py --region eu-west-1 --compliance nhs-dspt
python main.py --help
```

Every session is stored in `advisor.db`, and reported as `Saved as conversation 12`.
Pass `--no-save` to skip that. What the CLI spends counts against the same daily
ceiling as the web app: one API key, one budget.

To read that budget back out:

```bash
python store.py --report                  # this month, by day and by kind
python store.py --report --month 2026-07
```

## Web app

```bash
python server.py
```

Then open <http://127.0.0.1:5000>.

Five environment variables adjust how it runs:

| Variable               | Effect                                                        |
|------------------------|----------------------------------------------------------------|
| `ADVISOR_PORT`         | Serve on a different port (default `5000`)                    |
| `ADVISOR_DEBUG=1`      | Auto-reload on edits, and enable the Werkzeug debugger         |
| `ADVISOR_LOG`          | Log level for what each call cost and how long it took (`INFO`) |
| `ADVISOR_DAILY_TOKENS` | Tokens allowed per day (default `500000`; `0` removes the cap) |
| `ADVISOR_SECRET_KEY`   | Keeps session cookies valid across a restart                   |

Leave `ADVISOR_DEBUG` unset unless you are working on the app. It turns on the
interactive debugger, which will run arbitrary code sent from the browser, and the
reloader, which drops requests that are in flight when it restarts.

Built to a single set of design tokens: four colours in `branding.py` set the whole
scheme, and every other shade is worked out from them. See
[Making it yours](#making-it-yours). Each recommendation is broken out into a
services table, an editable list of what it assumes, Well-Architected notes, a priced
monthly cost and a rendered architecture diagram, rather than a wall of Markdown. There is a **Compare** tab for putting two to
four workloads side by side, a session list so you can move between conversations, a button
to save the current one to JSON, and an **Export deliverable** dialog that writes the
review out as a PDF, a web page and Markdown.

Replies stream. A recommendation appears field by field as Claude writes it -- the
headline first, then the services, the cost tier, and the diagram last -- rather than
sitting behind a progress indicator for twenty seconds. A comparison streams every
column at once.

A conversation you have not saved is kept in the browser as you go, so a reload picks
it up where you left it, including a question you were part way through asking. Saving
is what makes one durable: that writes it to the store on the server, where the CLI's
conversations are too.

### Exporting a deliverable

**Export deliverable** in the sidebar turns what is on screen into files a client can
keep. It is disabled until a reply has rendered, because there is nothing to hand over
from an empty session.

The dialog opens with what the deliverable covers: the architecture's headline, its region,
how many services it has and what it costs. Under that it says whether the architecture is
current, or how many turns have been asked since it was written, with a button to ask for it
again. It also says what the document will be missing before you find out by opening it, so
an architecture the Price List could not reach is flagged as carrying the advisor's own
figures rather than looked-up rates.

| File          | What it is                                                              |
|---------------|-------------------------------------------------------------------------|
| `report.pdf`  | A4, branded cover in the accent colour, running header and footer                       |
| `report.html` | One self-contained page: fonts, logo and diagram inlined, opens offline |
| `report.md`   | CommonMark with pipe tables, for Confluence, Notion or a repo           |
| `diagram.svg` | The architecture as vector, beside the Markdown that points at it       |
| `diagram.png` | The same, rasterised, where there is a browser to do it (F7)             |
| `session.json`| Optional. The raw exchange, for reloading into the Advisor              |
| `terraform/`  | Optional. A Terraform module for the architecture (F4)                  |

One self-contained format downloads as that file; anything more travels as a zip of a
folder named `architecture-review-{client}-{date}`. Markdown and the Terraform module are
never single files -- one points at the diagram beside it, the other is a directory -- so
choosing either zips the folder on its own. The dialog also takes who the
document is for and who prepared it, and whether the brief, the transcript and the
diagram source travel as appendices.

### The diagram

Laid out here rather than by Mermaid: the graph is read out of the `flowchart LR` the model
writes, and drawn in the design system's own node styles. That is why there is no build step
and no runtime dependency in `static/`.

It draws three things Mermaid's own renderer would have drawn differently. Arrows carry
labels where the model names one. Nodes wrapped in a `subgraph` get a boundary drawn round
them -- a VPC, and availability zones nested inside it -- and those boxes are the union of
what is in them, so they grow and shrink with the graph rather than being sized in advance.
And the whole thing downloads as **SVG** or **PNG** from the head of the diagram card.

The boundaries rest on one decision worth knowing about. Nodes used to be centred within
their own column, which meant one logical row sat at a different height in every column;
they now sit on rows allocated for the whole graph, so a boundary's members occupy a
contiguous run of them and a box drawn round them cannot reach a node that is not one. A
graph that would need more rows than are readable drops its boundaries and falls back to
the old centred layout exactly, which is asserted by a test that compares the two.

### What it assumes

The model is asked for the three or four things it took as read -- traffic at peak, how much
data there is, the hours it has to be up -- and each one is stated as a figure you can
disagree with rather than a hedge. They are editable in place. Correcting one stages it, so
several can be put right in one pass; a notice then offers to rebuild the architecture on
them, which spends one review however many you changed. The correction goes into the thread
as a readable turn naming both the new figure and the old, so the transcript records what
was changed rather than only what it ended up as.

All three formats are written from one document model, so section 3 is the same section
in each. A section with no data behind it is left out rather than printed with "None": a
line the AWS Price List had no rate for carries the advisor's own figure and says so, and
one with neither is named rather than counted as nothing. The Well-Architected notes carry
the same pillar names and the same green and amber treatment they have on screen.

Conversations saved before the pricing work export too. Those predate the `region` field
and the per-service sizing the Price List is queried with, and neither is printed in the
document, so they are filled in rather than refused; where a review never stated a region,
the document says nothing rather than naming one. A conversation brought forward from the
Markdown era has no headline either, and is titled with the brief you wrote instead.

One thing to know about the figures. There is a single monthly number, and it is in US
dollars, because that is the currency AWS publishes list prices in. The document quotes
the same figure the screen does, and no exchange rate is invented anywhere in this app.

**The PDF needs a browser.** It is printed by headless Chrome or Edge, which keeps the
PDF and the web page the same document rather than two implementations that drift, and
adds no Python dependency. `/api/health` reports whether one was found, and the dialog
says so before you pick the format. Without one, export `report.html` and print it from
your own browser: it carries the same `@page` rules and gives the same pages. Set
`CHROME_PATH` if a browser is installed somewhere unusual.

### Well-Architected notes

The notes used to be a bullet list with a pillar abbreviation in front of each one. They
are a review of sorts now.

Each note is filed under one of the six pillars of the AWS Well-Architected Framework, is
labelled with the pillar's official name, and carries a link to that pillar's own
documentation. Three of the six are stored under a shorter name than AWS gives them, which
is why the mapping lives in `schema.py` and the display name is attached while the reply is
being rendered. The links come from that same table and are never read out of the reply: a
URL in a field description is an invitation to write a plausible one, and a citation nobody
can follow is worse than no citation at all.

A note is either something the architecture handles or something to go and look at, and it
carries the green or amber treatment for whichever it is. Under the notes is a coverage
count -- how many of the six pillars this reply spoke to, and which it did not. That count
is worked out on the server, so the screen and the exported document cannot disagree about
a number the reader is being shown. A note filed under something that is not a pillar still
appears; it just counts towards nothing, because dropping an observation to tidy a tally
would be the wrong trade.

Coverage is a measure of what was discussed, not a score. Six of six means the reply had
something to say about every pillar, and nothing more than that.

### Revising the architecture

The first reply is an architecture. Every turn after it used to be prose, which meant a
conversation could pin down the sizes, swap an engine or drop a service and the exported
deliverable would still describe the opening guess. That was the gap worth closing: an
opening brief is vague, so the first reply's sizes are the model's estimate, and what makes
a document worth handing to a client is what the conversation settled afterwards.

**Revise the architecture** asks for it again. It is a button of its own above the chips,
rather than third in a row of prompts that all look equally casual, and it also appears in
the export dialog when the conversation has moved on. Either way the same
thing happens: a fixed question goes in as a visible user turn, the reply comes back as a
full architecture rather than as prose, and it is drawn, priced and exported like the first
one. The earlier architecture stays in the thread, so you can see what moved.

Nothing infers that a follow-up meant to revise. The button says so, and that is the only
way it happens. Classifying every question would mean an architecture could change because
somebody asked what an ALB was, and one that changes behind your back is worse than one
that is out of date and says so.

The revision is asked for two things in particular, and both are in the system prompt where
they can be read: use what the conversation agreed rather than the estimate from the opening
brief, and change only what the conversation calls for. The second matters because a
revision restates the whole architecture, and redeciding the parts nobody mentioned is the
way this goes wrong.

The exported files say nothing about any of this. A document stamped "revision 3 of 3"
invites a client to ask about the two revisions they never saw, so the deliverable is the
architecture as it stands and nothing more.

### What it costs

The monthly figure is not the model's guess. The advisor says what it would run and how much
of it, and those lines are looked up in AWS's own published prices.

They come from the bulk Price List, which is a plain HTTPS GET and needs no AWS credentials:
one file per service per region. The files are large -- EC2 in London is 210 MB and 276,000
rows -- so they are streamed and filtered a row at a time rather than loaded, and what
survives is a few hundred rates that go into the store. A region takes about twelve seconds
to walk the first time and nothing after that.

```bash
python pricing.py --warm eu-west-2   # pull the prices ahead of time
python pricing.py --warm eu-west-2 --force   # fetch again even if they are fresh
```

Prices older than a week are refetched on their own, so warming is a convenience rather
than a chore. It is worth doing before a demo: pricing is a separate call from the
recommendation for exactly this reason, so the first architecture in a region waits on
AWS's files while every one after it waits for nothing.

Each line of the estimate ends up in one of three states, and which one it is in is never
hidden:

| State       | What it means                                                       |
|-------------|---------------------------------------------------------------------|
| `priced`    | The Price List had a rate. The rate wins, always.                   |
| `estimated` | No list price, so the advisor's own figure for that line is used.   |
| neither     | No list price and no figure. Counted as nothing, and named.         |

`estimated` is a flag of its own rather than the absence of `priced` because a zero is not
the same claim as a fifty-dollar one, and printing $0.00 as though it were an answer is the
failure this is here to avoid. Nine meters are priced (`Meter` in `schema.py`), which between
them cover most of what one of these architectures is billed for; a service that fits none of
them -- CloudFront, Route 53, a NAT gateway -- is where the advisor's own figure takes over.

There is one number to quote, and it is the total of those lines. The Low/Medium/High band
beside it is derived from that total, so the band and the figure cannot contradict each
other. The model's own tier is still asked for and still stored, but it is not displayed:
it is differenced against the arithmetic, and a tier two bands out is a mis-sized
architecture worth knowing about.

Everything here is on-demand list price in US dollars, before any discount, Savings Plan,
free tier or tiered rate. AWS bills in dollars and the advisor is asked for dollars, so no
exchange rate is invented anywhere.

### Region and compliance

Two selectors sit above the box you type in. Neither was an input before: the advisor
picked a region itself, and "NHS trust" or "payment data" in a brief changed nothing that
could be pointed at.

**Region** pins where the architecture runs, and with it what everything is priced
against. Left alone, the advisor still chooses -- London unless the workload argues for
somewhere else -- which is the right default and not a decision worth forcing.

**Compliance** names a regime it has to stand up to: UK data residency, PCI DSS, HIPAA or
NHS DSPT. Each one is a sentence in the prompt saying what to do rather than the name of a
standard to write a paragraph about, so what comes back is a different architecture and
not a disclaimer. The regime shows up in the services chosen and in the Well-Architected
notes, and it is printed on the deliverable as what the review was built for. The document
does not claim compliance with anything; that is not a claim a model gets to make.

Both are sent with the question rather than added to the system prompt, because the prompt
cache is a byte-for-byte prefix match and a region interpolated into it would void the
cache on every turn of every conversation. They stay visible for the whole conversation,
so a reader can see what the architecture on screen was built for, and a revision is built
to the same constraints. A saved conversation remembers the regime and puts the selector
back where it was.

### Comparing more than two

The **Compare** tab takes two to four workloads. **Add a workload** makes a column and
each one past the second can be removed again; four is the ceiling, where the columns stop
being readable and the wait stops being one anybody sits through. Every column is one API
call, so a four-way comparison counts as four against the rate limit rather than as one
request.

Each architecture is built to the same region and compliance profile, because comparing a
London architecture against an Oregon one is not a comparison. They stream at once, each
into its own column, and the columns share the grid's rows so the services, the notes and
the cost line up across all of them however uneven the content is. Past two columns the
row scrolls sideways rather than squeezing the diagrams into illegibility.

A comparison exports the way it always did, one section and one Terraform module an
option, up to `terraform-option-d`.

### Follow-up questions

The chips under a reply used to be the same three whatever was asked. They come from the
architecture on screen now, written in the same call that writes it, which costs nothing
extra and means the model has the whole thing in front of it: an architecture with a
single-AZ database can ask what happens when that data centre fails, one with a read
replica can ask whether it is still needed outside the busy period.

Three rules hold. A chip never asks what the brief already answered, never asks for the
architecture to be rebuilt -- that is a button, and a deliberate one -- and where nothing
usable comes back the generic three are shown instead, because an empty row where the
guidance should be is worse than a generic one. The chips are fixed at the moment the
architecture was written, so a conversation that has moved on has chips about the
architecture rather than about the last thing asked.

### The Terraform module

Ticking **Terraform module** puts a `terraform/` folder in the bundle, generated from the
architecture on screen. It is a starting point and nothing more: the dialog says so before
you tick it, every generated file repeats it, and its own `README.md` leads with it.

The sizes are the model's own. The instance type the estimate was priced against is the
instance type in the launch template, so the number in the report and the module on disk
describe the same infrastructure rather than two guesses at it.

| File                       | What is in it                                              |
|----------------------------|------------------------------------------------------------|
| `README.md`                | What it creates, what it does not, and what to fix first   |
| `providers.tf`             | Terraform and AWS provider versions, the shared tags       |
| `variables.tf`             | Every knob, each with a default that runs                  |
| `network.tf`               | VPC, subnets, gateways, route tables, flow logs            |
| `security.tf`              | One security group a tier, rules between them              |
| `main.tf`                  | The architecture itself, one block a recommended service    |
| `outputs.tf`               | Endpoints, names and the ARN of the database secret        |
| `terraform.tfvars.example` | Every default written out, ready to be argued with         |
| `src/{function}/`          | A placeholder handler, where the plan has a Lambda         |

What is generated is what the advisor sized: EC2 tiers as a launch template and an auto
scaling group, RDS instances with the engine and the Multi-AZ flag the meter implied,
ElastiCache as a replication group or a Memcached cluster, load balancers with a target
group and a listener, S3 buckets, and Lambda functions with a handler that runs. Around
them it writes a VPC with public, private and data subnets across two availability zones.

What is not generated is named rather than guessed at. A service the advisor could not
size -- CloudFront, Route 53, a NAT gateway priced as `unpriced` -- appears in the module's
README as a gap, with the reasoning the advisor gave for recommending it, and as a comment
at the top of `main.tf`. Wrong infrastructure-as-code is worse than none, so nothing is
invented to fill a hole.

Some things are decided here rather than by the model, because there is a defensible
default and the recommendation is silent: IMDSv2 required, Session Manager instead of SSH
keys and a bastion, encryption at rest wherever it is a flag, an RDS master password
generated into Secrets Manager so it never reaches the state file, and a data tier whose
route table has no way out. Each is a comment where it is not obvious.

One read is a guess, and it says so. RDS bills a read replica as an ordinary single-AZ
instance, so the service name is the only thing that distinguishes a replica from a second
primary. A service named like a copy of another database becomes `replicate_source_db`,
but only where the engine matches the primary's and is one that takes a replica without an
edition change. The resource carries a comment naming the service it was read from, and
the module's README lists it.

A comparison exports two modules, `terraform-option-a/` and `terraform-option-b/`, each
with its own resource-name prefix so applying both to one account is not a pile of
collisions.

`tests/test_terraform.py` runs `terraform fmt -check` over several generated modules
wherever the binary is installed, and `terraform init` plus `terraform validate` under
`pytest -m network`. Validation is what checks every resource and attribute against the
AWS provider's own schema, so the generator cannot drift from what the provider accepts
without a test going red.

### Saved conversations

The sidebar's **Saved** list shows everything in the store, newest first, including
conversations written by the CLI. Click one to reopen it, rebuilt into the view it was
captured from: an advisory session comes back as a conversation you can keep asking
questions in, and a comparison comes back side by side in the **Compare** tab.

The list is searchable. Type in the box above it to match a conversation's name or
anything said in it, and use the chips under that to narrow by cost band or by a tag you
added. A conversation can be tagged from its row; tags are yours, where the band and the
region are read off the architecture itself. What makes any of that a query rather than a
walk over every stored reply is three columns and two tables added for it, so a
conversation saved before this needs `python migrate.py --reindex` once to be findable
by band or by service.

Each row has three controls: download it as JSON, rename it, or delete it. A rename
changes one column and nothing else, so it can never collide with another conversation.

### Spend guards

Each recommendation costs real money, and the two ways to spend it by accident are a
loop in an open tab and a retry that never gives up. Three things bound that:

- **Per-tab and per-IP rate limits** on the four endpoints that cost something: the two
  that call the Claude API, plus pricing an architecture and writing an export, which spend
  AWS's bandwidth and a headless browser instead. A comparison counts as its own width,
  so a four-way one counts as four rather than as one request. Reading, searching, tagging
  and saving are not limited.
- **A daily token ceiling**, counted across the CLI and the web app together, which
  refuses the call with a plain explanation rather than letting it fail at the API.
  `/api/health` reports how much of it has gone.
- **A ledger**, one row per API call, so "what did this month cost" is a query -- and one
  that has an answer on screen. **What this has cost** in the sidebar, and `python store.py
  --report`, both split a month by day and by recommendation, comparison, revision and
  follow-up, and both name today's spend against the ceiling. It is not split by model: the
  ledger's `model` column records the configured model rather than the one that served the
  call, so a split on it would look precise and be wrong.

None of this is a security control and it is not meant to be: with no authentication
(S3 in `UPGRADE.md`) a determined client can clear its cookie and come back, and a
per-IP limit means little behind a shared address. What these bound is an accident.

### Security

There is no authentication, and the app is written for a laptop rather than a shared
address. Within that, a few things are deliberate:

- **Links in a reply are sanitised.** Only `http:`, `https:` and `mailto:` are rendered as
  links (`SAFE_SCHEMES` in `parse.py`). A `javascript:` or `data:` URL in a Markdown link
  executes if the scheme is left for the browser to work out, and prose from a model is
  still untrusted input.
- **A request body is capped** at 1 MB, refused with a sentence saying so rather than a
  stack trace.
- **A content security policy** is set on every response, which is why there are no inline
  event handlers anywhere in `static/` and a `<select>` is wired up with a `change`
  listener rather than a delegated click.
- **`/api/health` says nothing about the machine.** It reports whether the API key works,
  which model is in use, how much of the daily ceiling has gone and whether a browser was
  found for the PDF. It used to return the OS username to draw an avatar initial, which was
  harmless on one laptop and an information disclosure anywhere else.
- **The one generated secret stays out of the repository.** A Terraform module's RDS master
  password is generated into Secrets Manager rather than written into a variable or the
  state file.

What is not here is authentication (`S3` in `UPGRADE.md`) and a production WSGI server
(`A4`). Both belong before this runs anywhere but a laptop, and the spend guards are a poor
substitute for the first.

## Layout

| File           | Purpose                                                            |
|----------------|--------------------------------------------------------------------|
| `schema.py`    | What a recommendation is: the model Claude's reply is validated against |
| `advisor.py`   | Talking to the Claude API: prompt, streaming, cost accounting        |
| `store.py`     | Where conversations, spend and cached prices are kept (SQLite)       |
| `pricing.py`   | Prices an architecture against the AWS Price List (F1, F12)          |
| `parse.py`     | Renders a reply into the fields the web app draws                   |
| `export.py`    | Writes a review out as a PDF, a web page and Markdown (F3)          |
| `terraform.py` | Generates a Terraform module from a recommendation (F4)             |
| `branding.py`  | Company name, logo, and the colour scheme — the one file to rebrand  |
| `main.py`      | Command line interface                                              |
| `server.py`    | Web app: serves `static/` and streams `advisor.py` over SSE          |
| `migrate.py`   | One-off: brings conversations saved by an earlier version into the store |
| `static/`      | Front end (`index.html`, `app.css`, `app.js`, brand assets)         |
| `tests/`       | pytest suite; the browser suite is opt-in with `pytest -m slow`, and the Terraform and Price List checks with `pytest -m network` |

## Making it yours

Everything that identifies the tool as someone's — the name, the address, the logo and the
colours — lives in `branding.py`. Nothing else in the codebase carries a company name or a
colour as a literal, and there is no build step: edit the file, refresh the browser.

### The name, the logo and the paperwork

```python
BRAND_NAME = "Insert Company Name"  # cover pages, footers, the CLI banner, Terraform
BRAND_ADDRESS = "Insert Company Name Ltd, …"  # the footer of anything that leaves the building
BRAND_DOMAIN = "insert-domain.com"  # printed beside the address
PRODUCT_NAME = "Architecture Advisor"  # window title, CLI banner, Terraform header
DOC_PREFIX = "AR"  # document references: AR-2026-0819-01
```

For the logo, replace the two files in `static/assets/` and keep the names:

| File | Where it appears |
|---|---|
| `logo-placeholder.svg` | The sidebar, and the header, footer and cover of every export. Always drawn on a **dark** ground, so it needs light ink. |
| `mark-placeholder.svg` | The browser tab favicon, and the small avatar beside each reply. Always on a **light** ground. |

SVG keeps them readable in version control. PNGs work too — change `LOGO_TYPE` in `export.py`
to `"image/png"` so the exported files embed them with the right media type.

### The colour scheme

Four colours set everything:

```python
BRAND_PRIMARY = "#0F766E"  # buttons, links, tabs, focus rings, compute nodes
BRAND_SECONDARY = "#334155"  # the sidebar, and a diagram's entry points
BRAND_ACCENT = "#1E293B"  # export cover, hero band, data stores
BRAND_ON_DARK = "#7DD3C0"  # labels drawn ON the sidebar and the cover
```

Everything else is worked out from them — the button hover, the two gradients, the focus ring,
the drop shadow, the pale panel behind a brief, the muted tone in a diagram. You never set a
shade by hand.

Three rules are worth knowing before you pick:

- **Primary, secondary and accent all carry white text.** Buttons, the sidebar, the export
  cover and half the diagram print white on top, so all three need to stay dark. Aim for a
  contrast ratio of at least 4.5:1 against white — the shipped teal is 5.5:1.
- **`BRAND_ON_DARK` must be light.** It is the only one drawn *on* the dark surfaces, so it has
  to out-contrast the sidebar rather than blend into it. Lightening your primary is the easy
  choice, and what the shipped scheme does — it makes the sidebar labels echo the buttons.
- **Secondary and accent should be the same hue at two depths.** That is what makes the sidebar
  and an export cover read as one surface instead of two unrelated panels.

The green, amber and red are deliberately **not** yours to set. They say whether a
Well-Architected pillar is handled, needs a look, or whether an action destroys something, and
a reader knows those three colours before they know your brand. They live in `SEMANTIC` in
`branding.py` if you genuinely have to move them, but a rebrand should not.

The neutral chrome — body text, rules, panels, the page behind the app — is in `NEUTRALS`
just above it. Most schemes leave it alone.

To check a scheme before committing to it, set the four values and run `pytest
tests/test_design_tokens.py`. That will not judge your taste, but it does prove every colour
the app asks for is defined and that the web page, the diagram and the PDF agree.

### How it reaches the screen

Worth knowing if you are changing how any of this works, rather than just recolouring it:

```
branding.py  ──  server.py serves /brand.css  ──  app.css reads var(--cs-…)
             │                                └─  app.js reads the same tokens
             │                                    back with getComputedStyle
             └──  export.py imports palette() directly, because a PDF
                  has to open with no stylesheet
```

The stylesheet is generated per request rather than sitting in `static/`, which is what keeps
the three consumers from drifting apart. It also means the page has to be opened through the
running server — `python server.py`, then `http://127.0.0.1:5000` — not by double-clicking
`index.html`.

## How a recommendation is produced

```
browser / CLI
     |  the brief, plus region and compliance
     v
server.py  or  main.py          neither talks to the SDK, neither owns storage
     |
     v
advisor.py --------------------> Claude API      streamed, one call a turn
     |   kind decides the call:  structured and high effort for an architecture,
     |   prose and low effort for a follow-up, which may search AWS's own docs
     v
parse.py       the reply -> the fields the browser draws, streamed as partials
     |
     +--> pricing.py  --------> AWS bulk Price List -> one monthly figure
     |
     +--> store.py    --------> advisor.db: conversation, messages, ledger, facets
     |
     +--> export.py / terraform.py -> the PDF, the page, the Markdown, the module
```

`schema.py` defines a `Recommendation` -- a headline, an overview, what the sizing assumes,
a list of services, the Well-Architected notes, a cost tier and a Mermaid diagram -- and
that model is sent to the API as a JSON schema. The reply comes back validated against it rather than scraped out of
Markdown, so a change to the prompt cannot quietly break the rendering. Follow-up questions
are ordinary Markdown prose, which is what they read best as.

Four settings in `advisor.py` are worth knowing about:

- **Effort.** A first recommendation and a comparison run at `high`; a follow-up runs at
  `low`. They are not the same job, and effort is the biggest single lever on both the
  latency and the cost of a turn.
- **Prompt caching.** Each request marks its last block cacheable, so the conversation so
  far is what gets cached. The system prompt is one fixed string with nothing interpolated
  into it, because the cache is a byte-for-byte prefix match. Cache reads start on the
  second follow-up: the first turn has no prefix to match, and it is a structured call
  where every later turn is prose. `usage.cacheReadTokens` in the sidebar is the check.
- **Streaming.** Every call streams, which is what makes the progressive rendering possible
  and stops a long reply hitting an HTTP timeout.
- **Web search.** A follow-up can check AWS's own documentation before it answers, using
  Anthropic's server-side search tool. See below.

### Asking it something current

The model's knowledge has a cutoff and AWS ships constantly, so a follow-up question can go
and look. Anthropic runs the search, and the domains it may read are AWS's own pages
(`SEARCH_DOMAINS` in `advisor.py`), which is what makes a citation worth putting in front of
a client. Pages Claude used are numbered in the answer and listed under it as links, so a
figure can be traced to the page it came from.

Two things are worth knowing:

- **Only follow-ups search.** A cited answer and a schema-constrained one are mutually
  exclusive at the API, and a recommendation whose shape is guaranteed is worth more than a
  cited one. So the first recommendation is the model's own knowledge and a follow-up
  ("is that instance family still current?", "what does that cost now?") is where to ask.
- **A search is billed per request**, not per token, at $10 per thousand. The sidebar counts
  them separately from tokens, and the ledger stores them, so the running cost stays honest.
  A turn is capped at `MAX_SEARCHES` searches.

One setting is worth knowing about, because it is not the default. `allowed_callers` is set to
`["direct"]`, which turns off dynamic filtering -- the mode where Claude writes code to cut
the results down before they reach it, and which costs fewer input tokens. Verified against
the real API: a turn filtered that way answers from the pages and returns **no citations at
all**, because what reached the model was code output rather than search results. Citations
are the point of this feature, so it takes the tokens.

### When a reply fails

A call that goes wrong is told to you in a sentence, and the failures worth knowing about
are the ones that do not look like failures.

- **A truncated reply is a success at the API.** `max_tokens` is a hard cap on thinking and
  reply text together, so a reply can stop mid-sentence and come back with a perfectly
  normal status. The stop reason is checked on every call, and a reply that ran out of room
  says so and suggests a narrower question. A structured reply never gets that far, because
  half a JSON document fails validation first; both paths raise the same sentence.
- **A refusal, a context-window overflow and a paused turn** are each recognised and named
  rather than surfacing as a parse error.
- **A failed call still reports what it spent.** The tokens a truncated or refused reply
  consumed go to the ledger and the daily ceiling like any other, because they were billed
  like any other.
- **The daily ceiling refuses before it calls.** A turn that would go over says so, rather
  than failing at the API.

### Where conversations are kept

`advisor.db`, a SQLite database in the project root, with six tables: the conversations,
their messages, one row per API call, the prices pulled from AWS, the services each saved
architecture recommended, and the tags somebody put on it. It replaced a directory of
timestamped JSON files, which could not be listed without reading every one of them, could
not be searched, and used the filename as a primary key.

The last two, and the region, cost band and compliance columns beside them, are what make a
saved conversation findable. All but the compliance profile are read off the reply rather
than typed, which is why `migrate.py --reindex` can rebuild them.

The JSON has not gone anywhere. Every conversation can be downloaded from the sidebar, and
the whole store can be written back out:

```bash
python migrate.py --export out/   # one JSON file per conversation
```

Conversations saved by an earlier version are imported the same way round. They hold
Markdown where a recommendation now holds JSON, and `migrate.py` converts them as it goes:

```bash
python migrate.py --dry-run   # say what would happen
python migrate.py             # import conversations/, keeping the files as .json.imported
python migrate.py --dir old/  # read them from somewhere other than conversations/
python migrate.py --reindex    # rebuild what the sidebar's filters search on
```

An unimported file is not lost: it just is not in the app until you run that.

`--reindex` is the one to run after upgrading to a version with the searchable sidebar. It
re-reads the replies already in the store and puts each conversation's region, cost band
and service list back on it, which is what a filter matches against. It costs nothing --
no API call, no network -- and it is safe to run again whenever a doubt arises.

## Development

```bash
pytest                                 # the default run: no browser, no network
pytest tests/test_pricing.py           # one file
pytest -m slow                         # the browser suite (needs Chrome and a free port)
pytest -m network                      # the real Price List, and terraform init/validate
pytest -m ""                           # everything
ruff check . && ruff format .
mypy
```

No test touches the Claude API: the key is faked and `call_claude` is patched. Nor does the
default run reach the network at all, and that is enforced rather than intended -- the
`no_outbound_http` fixture in `tests/conftest.py` blocks `urlopen` for anything not marked
`network`. It was worth enforcing: `pricing.estimate` warms a region whose rates are not
cached, the throwaway store every test gets means they never are, and eight CLI tests were
each streaming AWS's real price list files. That was most of the suite's runtime.

The two opt-in marks are the ones that need something the machine may not have -- `slow`
drives Chrome over the DevTools protocol, and `network` fetches AWS's real Price List and
shells out to Terraform.

`.pre-commit-config.yaml` runs the three gates above, and `.github/workflows/ci.yml` runs
them plus `pytest` on every push, with `-m network` nightly. Install the hooks with
`pre-commit install`.

Several duplications are deliberate and each has a test guarding it, because each exists
for a reason a refactor would undo. The diagram palette is CSS custom properties in
`app.css` and hex literals in `app.js`, since an SVG presentation attribute cannot take a
`var()`. The diagram's layout is in `app.js` and hand-ported into `export.py`, so an
exported diagram is the one the app draws. The compare width bounds are in `server.py`,
`main.py` and `app.js`, with the server as the authority. And the regions, compliance
regimes and cost bands are in `schema.py` and copied into `app.js`, because the front end
has no build step to generate them from. `tests/test_design_tokens.py`,
`tests/test_export.py` and `tests/test_front_end_contract.py` fail when any of them drift --
the last two of those pairs had nothing checking them until recently, and adding a region
to `schema.Region` left the selector quietly not offering it.

`CLAUDE.md` documents the conventions and the reasoning behind them. `UPGRADE.md` is the
audit and roadmap this was built from: items carry IDs (`R1`, `F3`, `S4`), are marked
`[verified]` when confirmed by running the code and `[done]` when implemented, and commit
messages cite them. Everything described above is `[done]`. What is left there is a prompt
evaluation harness (`Q4`), CI (`Q2`), batch mode (`F11`), authentication (`S3`) and a
production server (`A4`).

## Limits of the advice

Worth being straight about, because a document that looks like a deliverable will be read
as one.

- **It is advisory.** Every recommendation should be reviewed by somebody accountable for
  the workload before anything is built on it.
- **The cost is a floor, not a quote.** On-demand list price in US dollars, before any
  discount, Savings Plan, free tier, tiered rate, data transfer between regions or support
  plan. A line the Price List had no rate for carries the advisor's own figure and says so;
  a line with neither is named rather than counted as nothing.
- **The sizing comes from a brief.** A vague brief gets an estimate, and an estimate
  presented as an instance type reads with the confidence of a fact. That is what the
  assumptions panel and the revision button are for.
- **Compliance is not claimed.** A compliance profile changes the architecture the advisor
  proposes; it is not an assessment, and no document produced here says a workload complies
  with anything.
- **The Terraform module is a starting point.** It is validated against the AWS provider's
  schema, never applied. Read it before you plan it.
- **The model has a knowledge cutoff.** A follow-up can go and check AWS's own pages, and
  cites what it read. A first recommendation cannot, because a guaranteed reply shape and a
  cited one are mutually exclusive at the API.
