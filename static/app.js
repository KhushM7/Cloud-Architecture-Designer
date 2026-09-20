/* Architecture Advisor - front end.
 *
 * A live conversation is autosaved to localStorage as it goes, so a reload does
 * not lose it (A3). Saving is what makes one durable and shareable: that writes
 * it to the store on the server, and the Saved list reads from there.
 * Everything that touches the Claude API goes through server.py.
 */

'use strict';

const EXAMPLES = [
  "We're building a patient records system for an NHS trust. It needs to handle 500 " +
    'concurrent users, store sensitive data, and have 99.9% uptime.',
  'A startup wants to build a serverless data pipeline that ingests IoT sensor data in ' +
    'real time, processes it, and feeds a live dashboard for operations teams.',
  'An e-commerce company expects a 10x traffic spike over Black Friday. Their current ' +
    'setup is a single EC2 instance and an RDS MySQL database. How should they prepare?',
];

/* The chips under a reply: cheap questions about the architecture you have.
   Rebuilding it is not one of them -- it is a full structured turn at high effort
   and it replaces what is on screen, so it sits on its own button rather than
   third in a row of prompts that all look equally casual. */
const SUGGESTIONS = [
  { text: 'Break down the cost' },
  { text: 'What are the risks?' },
  { text: 'Compare with a serverless option' },
];

const DEFAULT_MODEL = 'claude-sonnet-5';

/* How many architectures one comparison may weigh up (F9). The same two numbers
   server.py enforces; the server is the authority and this is what stops the UI
   offering something it would refuse. */
const MIN_COMPARE = 2;
const MAX_COMPARE = 4;

/* Where an architecture can be built, and what it can be asked to stand up to
   (F5). Both mirror schema.py: Region and Compliance. The advisor picks the
   region itself when none is chosen, which is why the first entry is empty
   rather than a default. */
const REGIONS = [
  { id: '', label: 'Region: advisor decides' },
  { id: 'eu-west-2', label: 'London (eu-west-2)' },
  { id: 'eu-west-1', label: 'Ireland (eu-west-1)' },
  { id: 'eu-central-1', label: 'Frankfurt (eu-central-1)' },
  { id: 'eu-north-1', label: 'Stockholm (eu-north-1)' },
  { id: 'us-east-1', label: 'N. Virginia (us-east-1)' },
  { id: 'us-west-2', label: 'Oregon (us-west-2)' },
  { id: 'ap-southeast-1', label: 'Singapore (ap-southeast-1)' },
  { id: 'ap-southeast-2', label: 'Sydney (ap-southeast-2)' },
];

const COMPLIANCE = [
  { id: 'none', label: 'No named regime' },
  { id: 'uk-data-residency', label: 'UK data residency' },
  { id: 'pci-dss', label: 'PCI DSS' },
  { id: 'hipaa', label: 'HIPAA' },
  { id: 'nhs-dspt', label: 'NHS DSPT' },
];

/* The cost bands a saved conversation can be filtered by (F8). schema.Tier, and
   the en dashes are the real character, as they are everywhere else. */
const TIERS = ['Low', 'Low–Medium', 'Medium', 'Medium–High', 'High'];

/* ------------------------------------------------------------------ *
 * State
 * ------------------------------------------------------------------ */

const state = {
  sessions: [],
  activeId: null,
  tab: 'advisor',
  compare: {
    // Two to four briefs (F9). A list rather than `a` and `b`, which is what
    // this was until a comparison could be wider than a pair.
    workloads: ['', ''],
    results: null, // one entry a column, each null until it arrives
    thinking: ['', ''],
    title: '', // named by the server, like a session is
    error: null,
    loading: false,
    usage: null,
  },
  // What the architecture has to be built for (F5), per tab rather than per
  // conversation: it is a decision about the work, and it usually holds across
  // several questions about the same estate.
  constraints: { region: '', compliance: '' },
  tagging: null, // conversation being tagged, while the input is open
  query: { text: '', tier: '', service: '', tag: '' }, // the sidebar filters (F8)
  tags: [], // every tag in use, for offering them
  searching: false,
  loading: false,
  // The reply currently arriving, if any: { message, thinking }. `message` is
  // the same shape as a finished one, with whatever fields have landed so far.
  streaming: null,
  alert: null, // { kind, title, text, actions }
  pending: null, // { content } - a message that failed and can be retried
  savedNote: null, // 'Saved as …', shown in the sidebar after a save
  health: { ok: true, model: DEFAULT_MODEL, exports: { pdf: true } },
  export: null, // the export dialog (F3), while it is open
  discard: null, // {what, run} while asking whether to lose unsaved work
  // The spend view (F14), while it is open: { month, report, loading }. Not in
  // snapshot() -- the ledger belongs to the server, like state.saved does.
  usage: null,
  showSource: {}, // messageId -> boolean
  // messageId -> { index: text }. An assumption the reader has rewritten but
  // not yet spent a rebuild on (F10). Staged rather than sent, so several can be
  // corrected in one pass and one revision carries them all.
  assumptionEdits: {},
  editingAssumption: null, // { id, index } while one row is open for editing
  sources: {}, // messageId -> the Mermaid the diagram was drawn from
  diagrams: {}, // messageId -> the parsed graph, for writing it out as a file (F7)
  diagramTitles: {}, // messageId -> the headline, which is what names that file
  estimates: {}, // messageId -> what it costs a month (F1, F12)
  // messageId -> { cost, sessionId, index }. What the cost element needs that
  // does not come back from /api/estimate: the advisor's own read, and where
  // in the thread this architecture sits, for the staleness check.
  costs: {},
  costOpen: {}, // messageId -> boolean, once the reader has chosen
  setupHtml: null,
  saved: [], // conversations/ on disk
  openedId: null, // which saved conversation is on screen
  renaming: null, // conversation being renamed
  deleting: null, // conversation awaiting delete confirmation
};

const $ = (id) => document.getElementById(id);

// index.html is static, so the shell's nodes are looked up once. Anything inside
// a rendered view has to be fetched on demand.
const dom = {
  sessions: $('sessions'),
  savedList: $('saved-list'),
  savedFilters: $('saved-filters'),
  exportBtn: $('export-btn'),
  saveBtn: $('save-btn'),
  savedBox: $('saved'),
  savedPath: $('saved-path'),
  modelLabel: $('model-label'),
  tabAdvisor: $('tab-advisor'),
  tabCompare: $('tab-compare'),
  advisorView: $('advisor-view'),
  compareView: $('compare-view'),
  toasts: $('toasts'),
  exportDialog: $('export-dialog'),
};

function newSession() {
  const session = {
    id: `s${Date.now()}`,
    title: '',
    createdAt: Date.now(),
    messages: [],
    usage: null,
  };
  state.sessions.unshift(session);
  state.activeId = session.id;
  return session;
}

/* A reopened saved conversation gets an id of `saved-<n>`; the live one you are
   working in is `s<millis>`. "This session" lists the live one and nothing else,
   so opening something out of Saved does not push the buttons off the sidebar. */
function isLive(session) {
  return !String(session.id).startsWith('saved-');
}

function liveSession() {
  return state.sessions.find((session) => isLive(session) && session.messages.length) || null;
}

/** Whether a session holds work that closing it would throw away.
 *
 * `savedId` is set when a conversation is written to the store, and `savedCount`
 * with it: asking two more questions after saving makes it unsaved again, which
 * a bare `savedId` check would miss.
 */
function isUnsaved(session) {
  if (!session || !session.messages.length) return false;
  if (!session.savedId) return true;
  return session.messages.length !== session.savedCount;
}

/** The live conversation that a new session, a clear or a reload would lose. */
function unsavedWork() {
  const session = liveSession();
  return session && isUnsaved(session) ? session : null;
}

function activeSession() {
  let session = state.sessions.find((s) => s.id === state.activeId);
  if (!session) session = state.sessions[0] || newSession();
  state.activeId = session.id;
  return session;
}

/* ------------------------------------------------------------------ *
 * Keeping a live conversation across a reload (A3)
 *
 * Anything not explicitly saved used to be gone the moment the tab reloaded,
 * which loses work and reads as the tool's fault. The whole live state goes
 * into localStorage after every change, and comes back on load.
 *
 * Only what is worth keeping is stored: not the alert on screen, not the reply
 * halfway through arriving, not the saved list (which the server owns).
 * ------------------------------------------------------------------ */

/* Namespaced so the app does not collide with anything else on the origin.
   Changing this string discards whatever every open tab is working on, so the
   shape is versioned below rather than renaming this. */
const STORE_KEY = 'advisor.live.v1';

// localStorage is a few megabytes and shared with everything else on the
// origin, so the newest conversations win and older ones are dropped rather
// than the write failing outright.
const STORE_BUDGET = 2000000;

let persistTimer = null;

/* What shape the stored payload is in. Bumped rather than changing STORE_KEY,
   because a new key silently discards whatever every open tab was working on;
   a version lets restore() bring the old shape forward instead. */
const STORE_VERSION = 3;

function snapshot() {
  const { workloads, results, usage, title } = state.compare;
  return {
    version: STORE_VERSION,
    savedAt: Date.now(),
    activeId: state.activeId,
    tab: state.tab,
    openedId: state.openedId,
    sessions: state.sessions.filter((session) => session.messages.length),
    compare: { workloads, results, usage, title },
    constraints: state.constraints,
    // A correction the reader has typed but not yet spent a rebuild on is work,
    // and closing the tab should not throw it away (F10).
    assumptionEdits: state.assumptionEdits,
  };
}

/** Bring a stored comparison forward to the current shape (F9). */
function migrateCompare(kept) {
  if (!kept || typeof kept !== 'object') return null;
  // Version 1 held two named fields where there is now a list. Assigning it
  // straight in would leave the state carrying both shapes at once.
  const workloads = Array.isArray(kept.workloads)
    ? kept.workloads.slice(0, MAX_COMPARE)
    : [kept.a || '', kept.b || ''];
  while (workloads.length < MIN_COMPARE) workloads.push('');
  return {
    workloads,
    results: Array.isArray(kept.results) ? kept.results.slice(0, MAX_COMPARE) : null,
    usage: kept.usage || null,
    title: kept.title || '',
  };
}

/** Write the live state out, dropping the oldest conversations if it will not fit. */
function persist() {
  if (persistTimer) {
    clearTimeout(persistTimer);
    persistTimer = null;
  }
  const kept = snapshot();
  try {
    let text = JSON.stringify(kept);
    while (text.length > STORE_BUDGET && kept.sessions.length > 1) {
      kept.sessions.pop(); // the list is newest first
      text = JSON.stringify(kept);
    }
    localStorage.setItem(STORE_KEY, text);
  } catch (e) {
    // Private browsing, a full quota, or storage turned off. Losing the
    // autosave is a shame; failing the interaction over it would be worse.
    try {
      localStorage.removeItem(STORE_KEY);
    } catch (ignored) {
      /* nothing more to try */
    }
  }
}

/** Coalesce the writes: render() is called far more often than state changes. */
function schedulePersist() {
  if (persistTimer) clearTimeout(persistTimer);
  persistTimer = setTimeout(() => {
    persistTimer = null;
    persist();
  }, 400);
}

/** Whatever was on screen when the tab was last open. Returns a draft, if any. */
function restore() {
  let kept;
  try {
    kept = JSON.parse(localStorage.getItem(STORE_KEY) || 'null');
  } catch (e) {
    return '';
  }
  if (!kept || !Array.isArray(kept.sessions)) return '';

  state.sessions = kept.sessions.filter(
    (session) => session && Array.isArray(session.messages) && session.messages.length
  );
  if (!state.sessions.length) return '';

  state.tab = kept.tab === 'compare' ? 'compare' : 'advisor';
  state.openedId = kept.openedId || null;
  const compare = migrateCompare(kept.compare);
  if (compare) {
    Object.assign(state.compare, compare);
    state.compare.thinking = state.compare.workloads.map(() => '');
  }
  // Version 2 and earlier had none of these, and an absent one is simply a
  // reader who has corrected nothing.
  if (kept.assumptionEdits && typeof kept.assumptionEdits === 'object') {
    state.assumptionEdits = kept.assumptionEdits;
  }
  if (kept.constraints) {
    state.constraints = {
      region: kept.constraints.region || '',
      compliance: kept.constraints.compliance || '',
    };
  }

  // An empty conversation is not stored, so an id that is not among them means
  // the tab was sitting on a fresh one -- someone had clicked New conversation.
  // Coming back to the previous thread instead would undo that.
  if (state.sessions.some((session) => session.id === kept.activeId)) {
    state.activeId = kept.activeId;
  } else {
    newSession();
    return '';
  }

  // A question the tab was reloaded in the middle of never got an answer.
  // Leaving it in the thread would be a lie about what was asked and answered,
  // so it comes back in the composer instead, ready to send again.
  const session = activeSession();
  const last = session.messages[session.messages.length - 1];
  if (last && last.role === 'user') {
    session.messages.pop();
    if (!session.messages.length) {
      state.sessions = state.sessions.filter((entry) => entry !== session);
    }
    return last.content;
  }
  return '';
}

/* ------------------------------------------------------------------ *
 * Helpers
 * ------------------------------------------------------------------ */

const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' };

function esc(text) {
  return text == null ? '' : String(text).replace(/[&<>"]/g, (character) => ESCAPES[character]);
}

// Formatters are expensive to construct, and every session and saved item needs
// one, so they are built once. All three are UK time.
const LONDON = { timeZone: 'Europe/London' };
const DAY_FORMAT = new Intl.DateTimeFormat('en-GB', {
  ...LONDON,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
});
const TIME_FORMAT = new Intl.DateTimeFormat('en-GB', {
  ...LONDON,
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
});
const SHORT_DAY_FORMAT = new Intl.DateTimeFormat('en-GB', {
  ...LONDON,
  day: 'numeric',
  month: 'short',
});

const DAY_MS = 86400000;
let dayKeys = { at: 0, today: '', yesterday: '' };

/** Today's and yesterday's day keys, refreshed at most once a minute. */
function relativeDays() {
  const now = Date.now();
  if (now - dayKeys.at > 60000) {
    dayKeys = {
      at: now,
      today: DAY_FORMAT.format(now),
      yesterday: DAY_FORMAT.format(now - DAY_MS),
    };
  }
  return dayKeys;
}

/** "Today, 16:48", "Yesterday, 17:01", "12 Aug, 16:50" - all UK time. */
function whenLabel(timestamp) {
  const date = new Date(timestamp);
  const key = DAY_FORMAT.format(date);
  const { today, yesterday } = relativeDays();

  const day =
    key === today ? 'Today' : key === yesterday ? 'Yesterday' : SHORT_DAY_FORMAT.format(date);
  return `${day}, ${TIME_FORMAT.format(date)}`;
}

/** Timestamps in saved files are ISO strings; sessions use epoch millis. */
function savedWhen(item) {
  const stamp = Date.parse(item.savedAt);
  return Number.isNaN(stamp) ? '' : whenLabel(stamp);
}

/* Naming a conversation used to happen here as well as in Python, with the
 * cut-off written out in both files and a comment asking whoever changed one to
 * change the other. The server names it and sends the name back (A5).
 */

function plural(count, word) {
  return `${count} ${word}${count === 1 ? '' : 's'}`;
}

/* ---- Token and cost accounting ---- */

/* Everything counted per call. Mirrors COUNT_FIELDS in advisor.py, including
   `searches`, which is billed per request rather than per token (F2). */
const COUNT_FIELDS = [
  'calls',
  'inputTokens',
  'outputTokens',
  'cacheReadTokens',
  'cacheWriteTokens',
  'searches',
];

/** Add one call's usage onto a running total. Mirrors merge_usage() in advisor.py. */
function addUsage(total, extra) {
  if (!extra) return total;
  const merged = { costUsd: 0, priced: true };
  COUNT_FIELDS.forEach((field) => {
    merged[field] = 0;
  });

  [total, extra].forEach((part) => {
    if (!part) return;
    COUNT_FIELDS.forEach((field) => {
      merged[field] += Math.max(0, Number(part[field]) || 0);
    });
    merged.costUsd += Math.max(0, Number(part.costUsd) || 0);
    merged.priced = merged.priced && part.priced !== false;
  });
  return merged;
}

function tokens(count) {
  return (Number(count) || 0).toLocaleString('en-GB');
}

/** The API bills in US dollars, so no exchange rate is guessed at here. */
function costLabel(usage) {
  if (!usage.priced) return 'unpriced model';
  const cost = Number(usage.costUsd) || 0;
  return `$${cost >= 1 ? cost.toFixed(2) : cost.toFixed(4)}`;
}

/* ------------------------------------------------------------------ *
 * Exporting a deliverable (F3)
 *
 * The dialog collects what the document needs that the conversation does not
 * hold -- who it is for, who wrote it, whether the transcript travels -- and
 * posts the conversation to /api/export, which sends back a file.
 *
 * The formats are described here rather than fetched, because they are a
 * property of the product and not of the server: what the server decides is
 * whether the PDF can be printed on this machine, which arrives with /api/health
 * as `exports.pdf`.
 * ------------------------------------------------------------------ */

const EXPORT_FORMATS = [
  {
    id: 'pdf',
    label: 'Branded PDF',
    file: 'report.pdf',
    hint: 'A4, cover page, running footer. The artefact a client keeps.',
  },
  {
    id: 'html',
    label: 'Web page',
    file: 'report.html',
    hint: 'One file. Fonts, logo and diagram inlined — opens offline, emails cleanly.',
  },
  {
    id: 'md',
    label: 'Markdown',
    file: 'report.md + diagram.svg',
    hint: 'Drops into Confluence, Notion or a repo. Diagram travels beside it.',
  },
  {
    id: 'json',
    label: 'Session data',
    file: 'session.json',
    hint: 'The raw exchange, for reloading into the Advisor. Not for the client.',
  },
  {
    id: 'tf',
    label: 'Terraform module',
    file: 'terraform/',
    hint:
      'A starting point, not a deployment. Sized from this architecture, and to be ' +
      'reviewed resource by resource before anyone runs it.',
  },
];

/* The formats that travel as a folder rather than as one file, so choosing one
   on its own still zips. Markdown points at the diagram beside it; the Terraform
   module is a directory. write() in export.py decides this too -- this only has
   to say the same thing, and test_export.py asserts they do. */
const FOLDER_FORMATS = ['md', 'tf'];

/* The turn the Revise button sends. A fixed sentence rather than something the
   user types, because the server must not have to guess which follow-up meant
   "change it": an architecture that moved because somebody asked what an ALB was
   is worse than one that never moves. It goes in as a real user message, so the
   thread and the transcript appendix both record why the architecture was
   restated, in words a client can read. */
const REVISE_TURN =
  'Revise the architecture to reflect everything we have agreed in this conversation.';

/* What the Rebuild-with-these-assumptions button sends (F10).
   Composed rather than fixed, because unlike REVISE_TURN it has something
   specific to say: which assumption was wrong and what it should be. It goes in
   as a real user message for the same reason that one does -- the thread and the
   transcript appendix both record why the architecture moved, in words a client
   can read -- and it names the old value beside the new so the record shows what
   was corrected rather than only what it ended up as. */
const MAX_ASSUMPTION_CHARS = 240;

const ASSUMPTION_TURN_HEAD =
  'These assumptions are wrong. Rebuild the architecture on the corrected ones:';

function assumptionTurn(edits) {
  const lines = edits.map(
    (edit) => `- ${edit.now}` + (edit.was ? ` (you had: ${edit.was})` : '')
  );
  return `${ASSUMPTION_TURN_HEAD}\n${lines.join('\n')}`;
}

/** Turn "&amp;lt;b&amp;gt;" back into text. The panel holds what parse.py escaped, and
    what goes to the model, into an input, or into the transcript is words. */
function unesc(text) {
  const box = document.createElement('textarea');
  box.innerHTML = String(text || '');
  return box.value;
}

/** The last architecture in a thread, which is the one a deliverable covers. */
function latestArchitecture(session) {
  for (let index = session.messages.length - 1; index >= 0; index -= 1) {
    const message = session.messages[index];
    if (message.role === 'assistant' && message.structured) return { message, index };
  }
  return null;
}

/** How many questions have been asked since the architecture was last stated.
 *
 * The staleness signal, and deliberately a count of turns rather than a judgement
 * about them: knowing whether "what does ALB stand for?" changed the architecture
 * needs a model, and the person reading the dialog already knows. So it reports
 * what it can prove and leaves the conclusion to them.
 */
function turnsSinceArchitecture(session) {
  const latest = latestArchitecture(session);
  if (!latest) return 0;
  return session.messages.slice(latest.index + 1).filter((m) => m.role === 'user').length;
}

/** Whether this machine can print a PDF, which is a browser being installed. */
function pdfAvailable() {
  return (state.health.exports || {}).pdf !== false;
}

/** Whether there is a reviewed architecture on screen to export at all. */
function exportable() {
  if (state.tab === 'compare') {
    return Boolean(state.compare.results && state.compare.results.some((r) => r && r.structured));
  }
  return activeSession().messages.some((m) => m.role === 'assistant' && m.structured);
}

/* The download's name, mirroring basename() and slug() in export.py. Kept in
   step by test_export.py and the browser suite, which assert the same string
   from both sides. */
function exportSlug(text) {
  return String(text || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 60)
    .replace(/^-+|-+$/g, '');
}

function exportBasename(dialog) {
  const compare = state.tab === 'compare';
  const parts = [compare ? 'architecture-options' : 'architecture-review'];
  const client = exportSlug(dialog.client);
  if (client) parts.push(client);
  else if (!compare) {
    const first = activeSession().messages.find((m) => m.role === 'assistant' && m.headline);
    parts.push(exportSlug(first ? first.headline : '') || 'advisor');
  }
  parts.push(todayIso());
  return parts.join('-');
}

/** Today, as the date a filename carries. Local rather than UTC: the day it was
    made, in the timezone of whoever made it. */
function todayIso() {
  const today = new Date();
  return [
    today.getFullYear(),
    String(today.getMonth() + 1).padStart(2, '0'),
    String(today.getDate()).padStart(2, '0'),
  ].join('-');
}

/** What the browser will save. One self-contained file keeps its own extension. */
function exportFilename(dialog) {
  const chosen = EXPORT_FORMATS.filter((f) => dialog.formats[f.id]);
  const stem = exportBasename(dialog);
  const single = chosen.length === 1 && !FOLDER_FORMATS.includes(chosen[0].id);
  return single ? `${stem}.${chosen[0].id}` : `${stem}.zip`;
}

function openExport() {
  if (!exportable()) return;
  state.export = {
    formats: { pdf: pdfAvailable(), html: true, md: true, json: false, tf: false },
    client: '',
    preparedBy: '',
    transcript: true,
    diagramSource: false,
    busy: false,
    error: null,
    note: null,
  };
  render();
  const field = $('export-client');
  if (field) field.focus();
}

function closeExport() {
  if (state.export && state.export.busy) return;
  state.export = null;
  render();
}

/** Read the dialog's fields off the DOM before a re-render throws them away. */
function captureExport() {
  const dialog = state.export;
  if (!dialog) return;
  const client = $('export-client');
  const preparedBy = $('export-prepared-by');
  if (client) dialog.client = client.value;
  if (preparedBy) dialog.preparedBy = preparedBy.value;
}

/* ---- What the deliverable covers ----
 *
 * The dialog used to let you choose formats without ever saying what was inside
 * them. This is the answer: the architecture, what it costs, and whether the
 * conversation has moved on since it was written. Every figure is already in
 * state -- the structured reply carries the headline, region and services, and
 * state.estimates carries the money -- so none of this costs a request.
 */

function subjectFacts(message, estimate) {
  const facts = [];
  if (message.region) facts.push(esc(message.region));
  const services = (message.services || []).length;
  if (services) facts.push(`${services} ${services === 1 ? 'service' : 'services'}`);
  // No fallback to the model's tier: the estimate is the only source of a cost
  // figure now, and where it has none the strip says nothing rather than
  // reaching for the guess this replaced.
  if (estimate && estimate.hasFigure && !estimate.pending && !estimate.error) {
    facts.push(
      `${money(estimate.monthlyUsd)}/mo ${estimate.anyEstimated ? 'estimated' : 'priced'}`
    );
  }
  return facts.join(' · ');
}

function subjectLine(icon, kind, text) {
  return (
    `<p class="subject__note subject__note--${kind}">` +
    `<span class="subject__icon" aria-hidden="true">${icon}</span>` +
    `<span>${text}</span></p>`
  );
}

/** The estimate for one architecture, by the key requestEstimate files it under. */
function estimateAt(session, index) {
  return state.estimates[`${session.id}-${index}`];
}

function compareSubject() {
  const results = state.compare.results || [];
  const rows = results
    .map((result, index) => {
      if (!result || !result.structured) return '';
      const estimate = state.estimates[`compare-${index}`];
      const figure =
        estimate && estimate.hasFigure && !estimate.pending && !estimate.error
          ? `<span class="subject__figure">${money(estimate.monthlyUsd)}/mo</span>`
          : '';
      return (
        `<p class="subject__option"><span class="subject__label">` +
        `${esc(String.fromCharCode(65 + index))}</span>` +
        `<span class="subject__title">${esc(result.headline || 'Untitled option')}</span>` +
        `${figure}</p>`
      );
    })
    .join('');

  // Compare has no follow-up composer, so a comparison cannot go stale.
  return (
    `<div class="subject">` +
    `<span class="eyebrow">This deliverable covers</span>` +
    rows +
    subjectLine('&#10003;', 'ok', 'Both options as they were generated.') +
    `</div>`
  );
}

function exportSubject(dialog) {
  if (state.tab === 'compare') return compareSubject();

  const session = activeSession();
  const latest = latestArchitecture(session);
  if (!latest) return '';

  const estimate = estimateAt(session, latest.index);
  const facts = subjectFacts(latest.message, estimate);
  const behind = turnsSinceArchitecture(session);

  let notes = '';
  if (behind) {
    // Its own panel rather than a third grey line: this is the one thing in the
    // dialog that can send a client an architecture the conversation has already
    // replaced, so it is the loudest thing in it.
    notes +=
      `<div class="subject__stale">` +
      `<div class="subject__stale-body">` +
      `<p class="subject__stale-title">` +
      `<span aria-hidden="true">&#9888;</span> Out of date</p>` +
      `<p class="subject__stale-text">` +
      `${behind} ${behind === 1 ? 'question has' : 'questions have'} been asked since this ` +
      `architecture was written. Anything agreed in ${behind === 1 ? 'it' : 'them'} ` +
      `— sizes, services, the diagram — is not in these files.</p></div>` +
      `<button class="btn btn--warn btn--sm" data-export-revise="1"` +
      `${dialog.busy ? ' disabled' : ''}>Rebuild it first</button>` +
      `</div>`;
  } else {
    notes += subjectLine('&#10003;', 'ok', 'The architecture as it now stands.');
  }

  if (!estimate || estimate.pending) {
    notes += subjectLine(
      '&#8505;',
      'info',
      'The monthly figure is still being worked out. Exporting now writes the ' +
        "advisor's own read of the cost and says there is no priced estimate."
    );
  } else if (estimate.error || !estimate.hasFigure) {
    notes += subjectLine(
      '&#8505;',
      'info',
      'Nothing here could be priced against the AWS Price List and the advisor put ' +
        "no figure on it either, so the document carries its band alone, unchecked."
    );
  } else if (estimate.anyEstimated) {
    notes += subjectLine(
      '&#8505;',
      'info',
      `${money(estimate.estimatedUsd)} of the ${money(estimate.monthlyUsd)} total is the ` +
        `advisor's own estimate rather than a published price, across ` +
        `${estimate.estimatedServices} of ${(latest.message.services || []).length} services. ` +
        'The document marks those lines.'
    );
  }

  return (
    `<div class="subject">` +
    `<span class="eyebrow">This deliverable covers</span>` +
    `<p class="subject__headline">${esc(latest.message.headline || 'Untitled architecture')}</p>` +
    (facts ? `<p class="subject__facts">${facts}</p>` : '') +
    notes +
    `</div>`
  );
}

function formatRow(format, dialog) {
  const on = Boolean(dialog.formats[format.id]);
  const off = format.id === 'pdf' && !pdfAvailable();
  const hint = off
    ? 'No browser was found to print a PDF. Export the web page and print it yourself.'
    : format.hint;
  return (
    `<label class="format${on && !off ? ' is-on' : ''}${off ? ' is-off' : ''}">` +
    `<input type="checkbox" data-export-format="${format.id}"${on && !off ? ' checked' : ''}` +
    `${off ? ' disabled' : ''}>` +
    `<span class="format__box" aria-hidden="true">✓</span>` +
    `<span class="format__body">` +
    `<span class="format__name"><span class="format__label">${esc(format.label)}</span>` +
    `<span class="format__file">${esc(format.file)}</span></span>` +
    `<span class="format__hint">${esc(hint)}</span></span></label>`
  );
}

function toggleRow(id, label, on) {
  return (
    `<label class="toggle"><input type="checkbox" data-export-toggle="${id}"` +
    `${on ? ' checked' : ''}><span class="toggle__track" aria-hidden="true"></span>` +
    `<span>${esc(label)}</span></label>`
  );
}

/* Asking before work is lost. Deliberately not window.confirm: it cannot say
   what is at stake, and it cannot offer to save on the way out. */
function renderDiscard() {
  const host = $('discard-dialog');
  if (!host) return;
  const dialog = state.discard;
  if (!dialog) {
    host.innerHTML = '';
    return;
  }

  const session = liveSession();
  const messages = session ? session.messages.length : 0;
  const architectures = session
    ? session.messages.filter((m) => m.role === 'assistant' && m.structured).length
    : 0;
  const worth = [
    plural(messages, 'message'),
    architectures ? plural(architectures, 'architecture') : '',
  ]
    .filter(Boolean)
    .join(' · ');

  host.innerHTML =
    `<div class="overlay" data-discard-backdrop="1">` +
    `<div class="dialog dialog--narrow" role="dialog" aria-modal="true" ` +
    `aria-labelledby="discard-title">` +
    `<div class="dialog__head">` +
    `<span class="eyebrow eyebrow--danger">Not saved</span>` +
    `<h2 class="dialog__title" id="discard-title">This conversation will be lost</h2>` +
    `<p class="dialog__lead">You are about to ${esc(dialog.what)}, and this ` +
    `conversation has not been saved. Nothing here is kept unless you save it.</p></div>` +
    `<div class="dialog__body">` +
    `<div class="subject">` +
    `<span class="eyebrow">What you would lose</span>` +
    `<p class="subject__headline">${esc((session && session.title) || 'Untitled workload')}</p>` +
    (worth ? `<p class="subject__facts">${esc(worth)}</p>` : '') +
    `</div></div>` +
    `<div class="dialog__foot">` +
    `<button class="btn btn--ghost btn--alert" data-discard-cancel="1">Keep working</button>` +
    `<button class="btn btn--ghost btn--alert btn--danger" data-discard-anyway="1">` +
    `Lose it</button>` +
    `<button class="btn btn--primary btn--alert" data-discard-save="1">Save first</button>` +
    `</div></div></div>`;
}

function renderExport() {
  const host = $('export-dialog');
  if (!host) return;
  const dialog = state.export;
  if (!dialog) {
    host.innerHTML = '';
    return;
  }

  const chosen = EXPORT_FORMATS.filter((f) => dialog.formats[f.id]).length;
  const compare = state.tab === 'compare';
  const lead = compare
    ? 'Both options on screen — the architectures, their notes and what each costs — ' +
      'written out as one client-ready document.'
    : 'Everything on screen — the recommendation, diagram, services, Well-Architected ' +
      'notes and priced estimate — written out as client-ready files.';

  const caution = dialog.formats.tf
    ? `<p class="dialog__note">${esc(
        'The Terraform module is generated from this architecture and has not been ' +
          'applied to any account. Treat it as a first draft: read every resource, ' +
          'check the sizes against your own numbers, and run terraform plan before ' +
          'terraform apply.'
      )}</p>`
    : '';

  const note = dialog.error
    ? `<p class="dialog__note dialog__note--error">${esc(dialog.error)}</p>`
    : dialog.note
      ? `<p class="dialog__note">${esc(dialog.note)}</p>`
      : '';

  host.innerHTML =
    `<div class="overlay" data-export-backdrop="1">` +
    `<div class="dialog" role="dialog" aria-modal="true" aria-labelledby="export-title">` +
    `<div class="dialog__head">` +
    `<span class="eyebrow eyebrow--primary">Handover</span>` +
    `<h2 class="dialog__title" id="export-title">Export deliverable</h2>` +
    `<p class="dialog__lead">${esc(lead)}</p></div>` +
    `<div class="dialog__body">` +
    exportSubject(dialog) +
    `<span class="eyebrow">Formats</span>` +
    EXPORT_FORMATS.map((format) => formatRow(format, dialog)).join('') +
    `<div class="dialog__fields">` +
    `<div class="dialog__field"><span class="eyebrow">Prepared for</span>` +
    `<input id="export-client" maxlength="120" placeholder="Client name" ` +
    `value="${esc(dialog.client)}" autocomplete="off"></div>` +
    `<div class="dialog__field"><span class="eyebrow">Prepared by</span>` +
    `<input id="export-prepared-by" maxlength="120" placeholder="You, Insert Company Name" ` +
    `value="${esc(dialog.preparedBy)}" autocomplete="off"></div></div>` +
    `<div class="toggles">` +
    toggleRow('transcript', 'Include the brief and full transcript as an appendix', dialog.transcript) +
    toggleRow('diagramSource', 'Include diagram source', dialog.diagramSource) +
    `</div>${caution}${note}</div>` +
    `<div class="dialog__foot">` +
    `<span class="dialog__name">${esc(exportFilename(dialog))}</span>` +
    `<button class="btn btn--ghost btn--alert" data-export-cancel="1"` +
    `${dialog.busy ? ' disabled' : ''}>Cancel</button>` +
    `<button class="btn btn--primary btn--alert" data-export-go="1"` +
    `${chosen && !dialog.busy ? '' : ' disabled'}>` +
    (dialog.busy ? 'Writing…' : `Export ${plural(chosen, 'file')}`) +
    `</button></div></div></div>`;
}

/** The conversation as the export endpoint wants it, with its cached estimates. */
function exportPayload(dialog) {
  const compare = state.tab === 'compare' && state.compare.results;
  const session = activeSession();

  const messages = compare
    ? state.compare.results.flatMap((result, index) => [
        { role: 'user', content: state.compare.workloads[index] || '' },
        { role: 'assistant', content: result.raw },
      ])
    : history(session.messages);

  // One estimate per architecture, in the order the server will find them.
  // The keys are the ones requestEstimate() files them under.
  const estimates = [];
  if (compare) {
    estimates.push(state.estimates['compare-0'] || null, state.estimates['compare-1'] || null);
  } else {
    session.messages.forEach((message, index) => {
      if (message.role === 'assistant' && message.structured) {
        estimates.push(state.estimates[`${session.id}-${index}`] || null);
      }
    });
  }

  return {
    messages,
    mode: compare ? 'compare' : 'advise',
    formats: EXPORT_FORMATS.filter((f) => dialog.formats[f.id]).map((f) => f.id),
    client: dialog.client,
    preparedBy: dialog.preparedBy,
    transcript: dialog.transcript,
    diagramSource: dialog.diagramSource,
    // Recorded in the document, not claimed by it: the deliverable says what
    // the architecture was asked to stand up to (F5).
    compliance: state.constraints.compliance === 'none' ? '' : state.constraints.compliance,
    estimates: estimates.map((estimate) =>
      estimate && !estimate.pending && !estimate.error ? estimate : null
    ),
  };
}

/** Hand a file the server built to the browser's own download machinery. */
function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  // Revoked on the next tick rather than immediately: Safari has not started
  // reading the blob by the time click() returns.
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

/** Close the dialog and ask for the architecture again, up to date.
 *
 * The dialog does not come back by itself when the revision lands. Reading the
 * revised architecture before sending it to a client is the point, and having to
 * press Export again is what makes that happen.
 */
function reviseFromExport() {
  const dialog = state.export;
  if (!dialog || dialog.busy) return;
  state.export = null;
  send(REVISE_TURN, true);
}

async function runExport() {
  const dialog = state.export;
  if (!dialog || dialog.busy) return;
  captureExport();
  dialog.busy = true;
  dialog.error = null;
  dialog.note = null;
  render();

  try {
    const response = await fetch('/api/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(exportPayload(dialog)),
    });

    if (!response.ok) {
      let failure = { error: `The export failed (${response.status}).` };
      try {
        failure = await response.json();
      } catch (ignored) {
        /* A failure with no JSON body: the status is all there is. */
      }
      dialog.busy = false;
      dialog.error = failure.error || 'The export failed.';
      render();
      return;
    }

    const filename = disposition(response) || exportFilename(dialog);
    saveBlob(await response.blob(), filename);

    const missed = response.headers.get('X-Export-Note');
    state.export = null;
    render();
    toast(missed ? `${filename} — ${missed}` : filename, {
      label: 'Saved',
      action: null,
    });
  } catch (failure) {
    dialog.busy = false;
    dialog.error = 'The export could not be sent. Check the connection and try again.';
    render();
  }
}

/** The filename the server chose, which is the one that ends up in Downloads. */
function disposition(response) {
  const header = response.headers.get('Content-Disposition') || '';
  const match = /filename="([^"]+)"/.exec(header);
  return match ? match[1] : '';
}

/* ------------------------------------------------------------------ *
 * API
 * ------------------------------------------------------------------ */

async function request(path, body, method) {
  let response;
  try {
    response = await fetch(path, {
      method: method || (body ? 'POST' : 'GET'),
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    // The server itself is unreachable, which reads the same way as a dropped
    // connection to the API.
    throw { kind: 'connection', error: 'Could not reach the advisor. Is the server running?' };
  }

  let payload = {};
  try {
    payload = await response.json();
  } catch (e) {
    /* Non-JSON error page. */
  }

  if (!response.ok) {
    throw {
      kind: payload.kind || 'error',
      error: payload.error || `The request failed (status ${response.status}).`,
      // A call can fail after the API has already billed for it -- a reply cut
      // off at the token cap, for one -- so the cost still comes back.
      usage: payload.usage,
    };
  }
  return payload;
}

/* Read a Server-Sent Event stream, handing each event to `onEvent`.
 *
 * The advisor's two API calls stream, so that a recommendation can be drawn as
 * it is written rather than after a minute of nothing. Failures still arrive
 * the two ways `request()` knows about: as a JSON body with an error status
 * when the request never got as far as the API, and as an `error` event when it
 * failed part way through. Both are thrown, so callers catch one thing.
 */
async function streamRequest(path, body, onEvent) {
  let response;
  try {
    response = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch (e) {
    throw { kind: 'connection', error: 'Could not reach the advisor. Is the server running?' };
  }

  if (!response.ok || !(response.headers.get('content-type') || '').includes('event-stream')) {
    let payload = {};
    try {
      payload = await response.json();
    } catch (e) {
      /* Non-JSON error page. */
    }
    throw {
      kind: payload.kind || 'error',
      error: payload.error || `The request failed (status ${response.status}).`,
      usage: payload.usage,
    };
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let failure = null;
  let finished = false;

  for (;;) {
    const chunk = await reader.read();
    if (chunk.done) break;
    buffer += decoder.decode(chunk.value, { stream: true });

    // Events are separated by a blank line; anything after the last one is a
    // partial frame and stays in the buffer until the rest of it arrives.
    let split;
    while ((split = buffer.indexOf('\n\n')) !== -1) {
      const frame = buffer.slice(0, split);
      buffer = buffer.slice(split + 2);
      const line = frame.split('\n').find((part) => part.startsWith('data:'));
      if (!line) continue;

      let event;
      try {
        event = JSON.parse(line.slice(5));
      } catch (e) {
        continue; // a frame we cannot read is not worth tearing the stream down for
      }
      if (event.type === 'error') failure = event;
      else if (event.type === 'done') finished = true;
      onEvent(event);
    }
  }

  if (failure) throw failure;
  if (!finished) {
    throw { kind: 'connection', error: 'The connection dropped before the reply finished.' };
  }
}

/* ------------------------------------------------------------------ *
 * Diagram
 *
 * The advisor returns a Mermaid `flowchart LR`. Rather than pull in a
 * rendering library, lay the parsed graph out in columns by depth and draw it
 * in the design system's own node styles.
 * ------------------------------------------------------------------ */

const NODE_HEIGHT = 42;
const NODE_GAP_Y = 16;
const COLUMN_GAP = 54;
const NODE_PAD_X = 34;
const NODE_MIN_W = 92;
const EDGE_LABEL_SIZE = 11;
const NODE_FONT_FAMILY = "'IBM Plex Sans Condensed', system-ui, sans-serif";
const NODE_FONT = `600 14px ${NODE_FONT_FAMILY}`;

/* The diagram writes SVG presentation attributes and canvas fill styles, and
   neither can read `var(--cs-primary)`. So the palette is read back off the
   document instead of being named again here: branding.py is the only place a
   colour is written down, /brand.css carries it to the browser, and this asks
   the browser what it resolved to.

   Read synchronously, at the top level, on purpose. Both stylesheets are
   render-blocking <link>s in <head> and this script is the last thing in
   <body>, so the custom properties are already applied by the time this runs --
   which is what lets NODE_STYLES stay a plain object and the render() at the
   foot of this file paint a restored diagram on the first pass. */
const CSS_VARS = getComputedStyle(document.documentElement);
const token = (name) => CSS_VARS.getPropertyValue(`--cs-${name}`).trim();

const INK = {
  white: token('surface'),
  line: token('line-strong'),
  secondary: token('secondary'),
  ink: token('ink'),
};
const NODE_STYLES = {
  standby: { fill: INK.white, stroke: INK.line, text: INK.secondary, dashed: true },
  entry: { fill: INK.secondary, stroke: INK.secondary, text: INK.white },
  cache: { fill: token('accent-soft'), stroke: INK.line, text: INK.ink },
  data: { fill: token('accent'), stroke: token('accent'), text: INK.white },
  compute: { fill: token('primary'), stroke: token('primary'), text: INK.white },
  plain: { fill: INK.white, stroke: INK.line, text: INK.ink },
};

const STANDBY_RE = /replica|standby|secondary|failover|backup|archive/;
const CACHE_RE = /cache|redis|memcach|elasticache/;
const DATA_RE = /rds|aurora|dynamo|timestream|redshift|documentdb|neptune|database|postgres|mysql|sql/;
const COMPUTE_RE = /ec2|lambda|fargate|\becs\b|\beks\b|auto scaling|\basg\b|batch|app runner|compute/;

/* Measuring a label means asking a canvas, which is the expensive part of laying
   a graph out, so the answers are kept. Dropped wholesale once there are more
   than a session's worth: what this exists for is making one layout pass cheap,
   a pass measures a few dozen labels, and a tab left open across many diagrams
   should not keep every label it has ever drawn. */
const MAX_MEASURED_LABELS = 500;
const labelWidths = new Map();
let measureContext = null;

function textWidth(label) {
  let width = labelWidths.get(label);
  if (width === undefined) {
    if (!measureContext) {
      measureContext = document.createElement('canvas').getContext('2d');
      measureContext.font = NODE_FONT;
    }
    width = measureContext.measureText(label).width;
    if (labelWidths.size >= MAX_MEASURED_LABELS) labelWidths.clear();
    labelWidths.set(label, width);
  }
  return width;
}

/** Pick a node's treatment from what the service is, per the design's palette. */
function nodeStyle(node) {
  const label = node.label.toLowerCase();
  if (STANDBY_RE.test(label)) return NODE_STYLES.standby;
  if (node.entry) return NODE_STYLES.entry;
  if (CACHE_RE.test(label)) return NODE_STYLES.cache;
  if (DATA_RE.test(label)) return NODE_STYLES.data;
  if (COMPUTE_RE.test(label)) return NODE_STYLES.compute;
  return NODE_STYLES.plain;
}

// Laying a graph out measures every label on a canvas, so the finished SVG is
// kept against the diagram it came from rather than rebuilt on every render.
const svgCache = new WeakMap();

function diagramSvg(diagram) {
  let svg = svgCache.get(diagram);
  if (svg === undefined) {
    svg = renderDiagram(diagram);
    svgCache.set(diagram, svg);
  }
  return svg;
}

/* Boundaries: a VPC, and availability zones inside it (F7).
 *
 * These constants are duplicated in export.py, like the ones above them, and
 * tests/test_export.py asserts the two copies still agree.
 */
const MAX_GROUP_LEVELS = 2;
const GROUP_PAD = 12;
const GROUP_HEAD = 20;
const GROUP_RADIUS = 10;
const MAX_BANDS = 6;

/** How much room a boundary needs around its members: more for an outer one. */
function groupPad(group) {
  return GROUP_PAD * (MAX_GROUP_LEVELS - group.level);
}

/* Rows, allocated once for the whole graph rather than per column.
 *
 * This is the whole trick, and the reason drawing a box round a group of nodes
 * is not a two-dimensional packing problem here. Today's engine is principled in
 * x -- the column is the node's depth -- and ad hoc in y: each column centres its
 * own nodes, so one logical row sits at a different height in every column. That
 * is what makes a box unsafe, because a box spanning columns two to five can
 * swallow a stranger sitting in column three.
 *
 * So y becomes a second, independent axis: a band, allocated for the whole graph
 * by walking the group tree depth-first. Every group's leaf descendants then own
 * a contiguous run of bands, and containment follows from that rather than being
 * hoped for -- a node in a band inside a group's range is owned by a descendant
 * of that group, which is to say it is one of its members. The check in
 * layoutDiagram is defence against a bug, not the thing keeping this correct.
 *
 * Returns null where the graph would need more bands than are readable, and the
 * caller then draws no boundaries at all and falls back to the centred layout.
 */
function assignBands(used, groups, groupOf) {
  // Depth-first, source order, and everything outside a boundary last.
  const owners = [];
  const walk = (parent) => {
    groups
      .filter((group) => group.parent === parent)
      .forEach((group) => {
        owners.push(group.id);
        walk(group.id);
      });
  };
  walk(null);
  owners.push('');

  const ownerOf = (node) => groupOf.get(node.id) || '';

  // What each owner needs is the most nodes it has in any one column.
  const rows = new Map(owners.map((key) => [key, 0]));
  used.forEach((column) => {
    const counts = new Map();
    column.forEach((node) => {
      const key = ownerOf(node);
      counts.set(key, (counts.get(key) || 0) + 1);
    });
    counts.forEach((count, key) => {
      if (count > (rows.get(key) || 0)) rows.set(key, count);
    });
  });

  let next = 0;
  const start = new Map();
  owners.forEach((key) => {
    start.set(key, next);
    next += rows.get(key) || 0;
  });
  if (next > MAX_BANDS) return null;

  const bandOf = new Map();
  used.forEach((column) => {
    const at = new Map();
    column.forEach((node) => {
      const key = ownerOf(node);
      const index = at.get(key) || 0;
      at.set(key, index + 1);
      bandOf.set(node.id, start.get(key) + index);
    });
  });
  return { bandOf, bands: next };
}

/** Every node inside a boundary, its own and its descendants'. */
function groupNodes(group, groups) {
  const inside = [...group.members];
  groups
    .filter((other) => other.parent === group.id)
    .forEach((child) => inside.push(...groupNodes(child, groups)));
  return inside;
}

/* Where everything goes, worked out once (F7).
 *
 * Split out of renderDiagram so the same geometry can be drawn twice: as SVG for
 * the page and the file, and onto a canvas for the PNG. The layout is the part
 * that has to agree between them, so it is the part that is shared; only the
 * drawing primitives differ.
 *
 * Returns null where there is nothing to draw.
 */
function layoutDiagram(diagram) {
  const nodes = (diagram && diagram.nodes) || [];
  if (!nodes.length) return null;

  const columns = [];
  nodes.forEach((node) => {
    (columns[node.depth] = columns[node.depth] || []).push(node);
  });
  const used = columns.filter(Boolean);

  const groupOf = new Map();
  let groups = (diagram && diagram.groups) || [];
  groups.forEach((group) => {
    group.members.forEach((id) => groupOf.set(id, group.id));
  });

  // No boundaries, or too many bands for the result to be readable: the centred
  // layout this engine has always drawn, unchanged to the pixel.
  let banding = groups.length ? assignBands(used, groups, groupOf) : null;
  if (!banding) {
    groups = [];
    banding = null;
  }

  const columnOf = new Map();
  used.forEach((column, index) => {
    column.forEach((node) => columnOf.set(node.id, index));
  });
  const inside = new Map(groups.map((group) => [group.id, groupNodes(group, groups)]));

  // Room for the labels, reserved before anything is placed. A gutter holds no
  // nodes, which is why a label goes in one -- but being in the gutter is not
  // enough, it has to fit, and a gutter is only COLUMN_GAP wide until something
  // asks for more. A named arrow asks, in the gap in front of the node it points
  // at, which is where its label is drawn.
  const gapNeed = used.map(() => 0);
  ((diagram && diagram.edges) || []).forEach((edge) => {
    if (!edge.label) return;
    const target = columnOf.get(edge.to);
    if (!target) return;
    const need = labelWidth(edge.label) + 8;
    if (need > gapNeed[target - 1]) gapNeed[target - 1] = need;
  });
  const gapAfter = (index) => Math.max(COLUMN_GAP, gapNeed[index]);

  // Room for the boundaries, reserved before anything is placed rather than
  // discovered afterwards: a box is drawn round its members, so the space it
  // needs has to already be between them.
  const leftPad = used.map(() => 0);
  const rightPad = used.map(() => 0);
  const above = [];
  const below = [];
  const span = new Map();

  groups.forEach((group) => {
    const members = inside.get(group.id);
    const cols = members.map((id) => columnOf.get(id)).filter((at) => at !== undefined);
    const bands = members.map((id) => banding.bandOf.get(id)).filter((at) => at !== undefined);
    if (!cols.length || !bands.length) return;
    const pad = groupPad(group);
    const first = Math.min(...cols);
    const last = Math.max(...cols);
    const top = Math.min(...bands);
    const bottom = Math.max(...bands);
    span.set(group.id, { first, last, top, bottom });
    leftPad[first] += pad;
    rightPad[last] += pad;
    above[top] = (above[top] || 0) + pad + GROUP_HEAD;
    below[bottom] = (below[bottom] || 0) + pad;
  });

  const layout = new Map();
  // Where each column ended up, so a label can be centred in the gap in front of
  // it however much padding a boundary put there.
  const colX = [];
  const colW = [];
  let height = 0;
  let width = 0;

  if (banding) {
    const bandY = [];
    let y = 0;
    for (let band = 0; band < banding.bands; band += 1) {
      y += above[band] || 0;
      bandY.push(y);
      y += NODE_HEIGHT + (below[band] || 0);
      if (band < banding.bands - 1) y += NODE_GAP_Y;
    }
    height = y;

    let x = 0;
    used.forEach((column, index) => {
      x += leftPad[index];
      const columnWidth = Math.max(
        NODE_MIN_W,
        ...column.map((node) => textWidth(node.label) + NODE_PAD_X)
      );
      colX[index] = x;
      colW[index] = columnWidth;
      column.forEach((node) => {
        layout.set(node.id, {
          x,
          y: bandY[banding.bandOf.get(node.id)],
          w: columnWidth,
          h: NODE_HEIGHT,
        });
      });
      x += columnWidth + rightPad[index];
      if (index < used.length - 1) x += gapAfter(index);
    });
    width = x;
  } else {
    const tallest = Math.max(...used.map((column) => column.length));
    height = tallest * NODE_HEIGHT + (tallest - 1) * NODE_GAP_Y;

    let x = 0;
    used.forEach((column, index) => {
      const columnWidth = Math.max(
        NODE_MIN_W,
        ...column.map((node) => textWidth(node.label) + NODE_PAD_X)
      );
      const columnHeight = column.length * NODE_HEIGHT + (column.length - 1) * NODE_GAP_Y;
      let y = (height - columnHeight) / 2;
      colX[index] = x;
      colW[index] = columnWidth;
      column.forEach((node) => {
        layout.set(node.id, { x, y, w: columnWidth, h: NODE_HEIGHT });
        y += NODE_HEIGHT + NODE_GAP_Y;
      });
      x += columnWidth + (index < used.length - 1 ? gapAfter(index) : 0);
    });
    width = x;
  }

  // The boxes themselves: the union of what is in them, and nothing fixed
  // anywhere. A node added, a label made longer, an availability zone dropped --
  // the union is different on the next render and the box is the size of
  // whatever it now holds. Innermost first, so an outer box can take its
  // children's boxes into its own union and is guaranteed to enclose them.
  const drawn = new Map();
  [...groups]
    .sort((a, b) => b.level - a.level)
    .forEach((group) => {
      const parts = group.members.map((id) => layout.get(id)).filter(Boolean);
      groups
        .filter((child) => child.parent === group.id)
        .forEach((child) => {
          const box = drawn.get(child.id);
          if (box) parts.push(box);
        });
      if (!parts.length) return;

      const pad = groupPad(group);
      const left = Math.min(...parts.map((box) => box.x)) - pad;
      const right = Math.max(...parts.map((box) => box.x + box.w)) + pad;
      const top = Math.min(...parts.map((box) => box.y)) - pad - GROUP_HEAD;
      const bottom = Math.max(...parts.map((box) => box.y + box.h)) + pad;
      drawn.set(group.id, {
        x: left,
        y: top,
        w: right - left,
        h: bottom - top,
        label: group.label,
        level: group.level,
      });
    });

  /* Defence against a bug rather than the thing keeping this honest -- see
     assignBands. A box that has caught a node it does not own is not drawn: the
     diagram is then missing a boundary, which is a smaller lie than a boundary
     drawn round the wrong thing. */
  const boundaries = [];
  groups.forEach((group) => {
    const box = drawn.get(group.id);
    if (!box) return;
    const own = new Set(inside.get(group.id));
    const swallowed = nodes.some((node) => {
      if (own.has(node.id)) return false;
      const at = layout.get(node.id);
      return (
        at &&
        at.x < box.x + box.w &&
        at.x + at.w > box.x &&
        at.y < box.y + box.h &&
        at.y + at.h > box.y
      );
    });
    if (!swallowed) boundaries.push(box);
  });

  // Where a label has already been drawn. Two edges crossing the same gutter on
  // the same row would write their labels on top of each other, so the first one
  // there keeps the slot and the rest become the arrow's tooltip: a label that
  // cannot be read is worse than one that is only in the source.
  const taken = new Set();
  const links = ((diagram && diagram.edges) || [])
    .map((edge) => {
      const from = layout.get(edge.from);
      const to = layout.get(edge.to);
      if (!from || !to || from === to) return null;

      const x1 = from.x + from.w;
      const y1 = from.y + from.h / 2;
      const x2 = to.x;
      const y2 = to.y + to.h / 2;
      const middle = x1 + (x2 - x1) / 2;
      // Straight where the rows line up, otherwise step through the gutter.
      const straight = Math.abs(y1 - y2) < 1;

      // One rule for both path shapes: centred in the gutter in front of the
      // node the arrow points at, just above the height it arrives at. That gap
      // was widened to fit it, it holds no nodes, and using the target's row
      // rather than the source's is what keeps a fan of arrows out of one
      // another's way -- five edges leaving one node arrive at five different
      // rows, so their labels stack instead of landing on the same spot.
      const target = columnOf.get(edge.to);
      const room = target ? to.x - (colX[target - 1] + colW[target - 1]) : 0;
      const label = edge.label && room > 24 ? fitLabel(edge.label, room) : '';
      let at = null;
      let hidden = edge.label && !label ? edge.label : '';
      if (label) {
        const where = { x: to.x - room / 2, y: y2 - 6 };
        const slot = `${Math.round(where.x / 8)}:${Math.round(where.y / 8)}`;
        if (taken.has(slot)) hidden = edge.label;
        else {
          taken.add(slot);
          at = where;
        }
      }
      return { x1, y1, x2, y2, middle, straight, label, at, hidden };
    })
    .filter(Boolean);

  const boxes = nodes.map((node) => ({
    ...layout.get(node.id),
    label: node.label,
    style: nodeStyle(node),
  }));

  return {
    width: Math.ceil(width),
    height: Math.ceil(height),
    nodes: boxes,
    edges: links,
    groups: boundaries,
  };
}

/* A boundary's name, cut to what its box can hold.
 *
 * Measured at the node font and scaled, because the canvas is only asked for one
 * font's metrics and this is drawn smaller than a node label. Erring wide means a
 * label is occasionally cut a character early, which is better than one that runs
 * out over the edge of the box it belongs to.
 */
function groupLabel(label, boxWidth) {
  const room = boxWidth - 20;
  const fits = (text) => (textWidth(text) * EDGE_LABEL_SIZE) / 14 <= room;
  if (fits(label)) return label;
  let cut = label;
  while (cut.length > 1 && !fits(`${cut}…`)) cut = cut.slice(0, -1);
  return `${cut}…`;
}

/* How wide a label renders.
 *
 * Measured at the node font and scaled, for the reason groupLabel gives: the
 * canvas is only ever asked for one font's metrics. Erring wide reserves a pixel
 * or two too much, which costs nothing. */
function labelWidth(text) {
  return (textWidth(text) * EDGE_LABEL_SIZE) / 14;
}

/* An edge label, cut to the gap it is drawn in.
 *
 * A backstop rather than the mechanism: the gutter is widened to fit the labels
 * crossing it, so this only bites where a label could not be given its own gap --
 * an arrow that skips a column, whose midpoint is over a node rather than in a
 * gutter. */
function fitLabel(label, room) {
  if (labelWidth(label) <= room - 8) return label;
  let cut = label;
  while (cut.length > 1 && labelWidth(`${cut}…`) > room - 8) cut = cut.slice(0, -1);
  return cut.length > 1 ? `${cut}…` : '';
}

/* Two dash patterns rather than two colours: nesting reads at a glance and the
   palette does not gain a member. Both of these are design tokens already. */
function groupDash(level) {
  return level === 0 ? '4 4' : '2 4';
}

/* The graph as SVG.
 *
 * `standalone` is the same flag export.py's diagram_svg carries and it means the
 * same thing: a file that opens with no stylesheet around it, so it declares the
 * namespace and paints its own ground. The --natural-width custom property goes
 * the other way -- it exists for app.css to scale the inline copy against, and
 * says nothing in a file.
 */
function renderDiagram(diagram, options) {
  const standalone = !!(options && options.standalone);
  const plan = layoutDiagram(diagram);
  if (!plan) return '';

  // First, so the nodes and the arrows paint over the boundary rather than
  // under it. Outermost first among themselves, for the same reason.
  const boundaries = plan.groups
    .map(
      (box) =>
        `<g><rect x="${box.x}" y="${box.y}" width="${box.w}" height="${box.h}" ` +
        `rx="${GROUP_RADIUS}" fill="none" stroke="${INK.line}" stroke-width="1" ` +
        `stroke-dasharray="${groupDash(box.level)}"/>` +
        `<text x="${box.x + 10}" y="${box.y + 13}" font-family="${NODE_FONT_FAMILY}" ` +
        `font-size="${EDGE_LABEL_SIZE}" font-weight="600" letter-spacing="0.04em" ` +
        `fill="${INK.secondary}">${esc(groupLabel(box.label, box.w))}</text></g>`
    )
    .join('');

  const edges = plan.edges
    .map((edge) => {
      const path = edge.straight
        ? `M${edge.x1} ${edge.y1} H${edge.x2 - 7}`
        : `M${edge.x1} ${edge.y1} H${edge.middle} V${edge.y2} H${edge.x2 - 7}`;
      const title = edge.hidden ? `<title>${esc(edge.hidden)}</title>` : '';
      // Haloed in the page's own white: the riser of a stepped arrow passes
      // through where the label sits, and paint-order puts the stroke behind the
      // glyphs so the line is broken by the words rather than drawn over them.
      const label = edge.at
        ? `<text x="${edge.at.x}" y="${edge.at.y}" text-anchor="middle" ` +
          `dominant-baseline="auto" font-family="${NODE_FONT_FAMILY}" ` +
          `font-size="${EDGE_LABEL_SIZE}" font-weight="400" fill="${INK.secondary}" ` +
          `stroke="${INK.white}" stroke-width="3" stroke-linejoin="round" ` +
          `paint-order="stroke">${esc(edge.label)}</text>`
        : '';
      return (
        `<path d="${path}" fill="none" stroke="${INK.line}" stroke-width="1.5" ` +
        `marker-end="url(#cs-arrow)">${title}</path>${label}`
      );
    })
    .join('');

  const boxes = plan.nodes
    .map((box) => {
      const dash = box.style.dashed ? ' stroke-dasharray="5 4"' : '';
      return (
        `<g><rect x="${box.x}" y="${box.y}" width="${box.w}" height="${box.h}" rx="8" ` +
        `fill="${box.style.fill}" stroke="${box.style.stroke}" stroke-width="1"${dash}/>` +
        `<text x="${box.x + box.w / 2}" y="${box.y + box.h / 2}" text-anchor="middle" ` +
        `dominant-baseline="central" font-family="${NODE_FONT_FAMILY}" ` +
        `font-size="14" font-weight="600" fill="${box.style.text}">${esc(box.label)}</text></g>`
      );
    })
    .join('');

  const w = plan.width;
  const h = plan.height;
  // The graph's natural width, published for app.css to scale against: on a wide
  // card the diagram grows into the space instead of sitting in the middle of it,
  // but only up to a multiple of this, so a three-node graph stays sensible.
  return (
    `<svg ${standalone ? 'xmlns="http://www.w3.org/2000/svg" ' : ''}` +
    `width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" ` +
    (standalone ? '' : `style="--natural-width:${w}px" `) +
    `aria-label="Architecture diagram"><defs>` +
    `<marker id="cs-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" ` +
    `orient="auto-start-reverse"><path d="M0 0 L8 4 L0 8 z" fill="${INK.line}"/></marker>` +
    `</defs>` +
    (standalone ? `<rect width="${w}" height="${h}" fill="${INK.white}"/>` : '') +
    `${boundaries}${edges}${boxes}</svg>`
  );
}

/* The diagram as a file (F7).
 *
 * Rendered rather than read back off the page: what is on screen is scaled by
 * app.css and carries a custom property that means nothing outside it. It goes
 * through renderDiagram directly and not diagramSvg, because svgCache is keyed on
 * the parsed diagram alone -- there is nowhere in that key to say which of the
 * two variants was asked for, and caching the standalone one under it would
 * poison the inline render.
 */
function downloadDiagramSvg(id) {
  const diagram = state.diagrams[id];
  if (!diagram) return;
  const svg = renderDiagram(diagram, { standalone: true });
  if (!svg) return;
  saveBlob(new Blob([svg], { type: 'image/svg+xml;charset=utf-8' }), diagramFilename(id, 'svg'));
}

/* And as a raster, at twice the device's pixel ratio because it ends up in a
   client deck. See paintDiagram for why it is painted rather than rasterised. */
const PNG_SCALE = 2;

async function downloadDiagramPng(id) {
  const diagram = state.diagrams[id];
  if (!diagram) return;
  const plan = layoutDiagram(diagram);
  if (!plan) return;

  // The labels are drawn in the page's own font, so the page has to have it.
  if (document.fonts && document.fonts.ready) await document.fonts.ready;

  const ratio = (window.devicePixelRatio || 1) * PNG_SCALE;
  const canvas = document.createElement('canvas');
  canvas.width = Math.ceil(plan.width * ratio);
  canvas.height = Math.ceil(plan.height * ratio);
  const pen = canvas.getContext('2d');
  if (!pen) return;
  pen.scale(ratio, ratio);
  paintDiagram(pen, plan);

  const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/png'));
  if (blob) saveBlob(blob, diagramFilename(id, 'png'));
}

/* What the file is called. `diagram.svg` is the right name inside a zip, where
   the report beside it is the context; in a Downloads folder among a hundred
   other things it is not, so this says what it is a diagram of. Mirrors
   basename() and slug() in export.py, through exportSlug. */
function diagramFilename(id, extension) {
  const parts = ['architecture-diagram'];
  const named = exportSlug(state.diagramTitles[id] || '');
  if (named) parts.push(named);
  parts.push(todayIso());
  return `${parts.join('-')}.${extension}`;
}

const ARROW = 8;

/** One arrowhead, filled, pointing right at the node it stops short of. */
function paintArrow(pen, x, y) {
  pen.beginPath();
  pen.moveTo(x - ARROW, y - ARROW / 2);
  pen.lineTo(x, y);
  pen.lineTo(x - ARROW, y + ARROW / 2);
  pen.closePath();
  pen.fill();
}

/* The same graph, painted onto a canvas for the PNG (F7).
 *
 * The third drawing of one layout -- this, the SVG above, and export.py's port --
 * and the reason it earns its place is the font: an SVG loaded as an image is its
 * own document and never gets this page's @font-face, so a rasterised one comes
 * out in the fallback face. Painting here uses the font the page has loaded.
 */
function paintDiagram(pen, plan) {
  pen.fillStyle = INK.white;
  pen.fillRect(0, 0, plan.width, plan.height);
  pen.lineJoin = 'round';

  plan.groups.forEach((box) => {
    pen.strokeStyle = INK.line;
    pen.lineWidth = 1;
    pen.setLineDash(groupDash(box.level).split(' ').map(Number));
    pen.beginPath();
    if (pen.roundRect) pen.roundRect(box.x, box.y, box.w, box.h, GROUP_RADIUS);
    else pen.rect(box.x, box.y, box.w, box.h);
    pen.stroke();
    pen.setLineDash([]);

    pen.fillStyle = INK.secondary;
    pen.font = `600 ${EDGE_LABEL_SIZE}px ${NODE_FONT_FAMILY}`;
    pen.textAlign = 'left';
    pen.textBaseline = 'alphabetic';
    pen.fillText(groupLabel(box.label, box.w), box.x + 10, box.y + 13);
  });

  plan.edges.forEach((edge) => {
    pen.strokeStyle = INK.line;
    pen.fillStyle = INK.line;
    pen.lineWidth = 1.5;
    pen.beginPath();
    pen.moveTo(edge.x1, edge.y1);
    if (edge.straight) {
      pen.lineTo(edge.x2 - ARROW, edge.y1);
    } else {
      pen.lineTo(edge.middle, edge.y1);
      pen.lineTo(edge.middle, edge.y2);
      pen.lineTo(edge.x2 - ARROW, edge.y2);
    }
    pen.stroke();
    paintArrow(pen, edge.x2, edge.straight ? edge.y1 : edge.y2);

    if (edge.at) {
      pen.font = `400 ${EDGE_LABEL_SIZE}px ${NODE_FONT_FAMILY}`;
      pen.textAlign = 'center';
      pen.textBaseline = 'alphabetic';
      // The halo first, then the words on top of it. Same effect as paint-order
      // in the SVG, which a canvas has no equivalent of.
      pen.strokeStyle = INK.white;
      pen.lineWidth = 3;
      pen.lineJoin = 'round';
      pen.strokeText(edge.label, edge.at.x, edge.at.y);
      pen.fillStyle = INK.secondary;
      pen.fillText(edge.label, edge.at.x, edge.at.y);
    }
  });

  plan.nodes.forEach((box) => {
    pen.fillStyle = box.style.fill;
    pen.strokeStyle = box.style.stroke;
    pen.lineWidth = 1;
    pen.setLineDash(box.style.dashed ? [5, 4] : []);
    pen.beginPath();
    // roundRect is everywhere this app runs, but a square-cornered node is a
    // better failure than none at all.
    if (pen.roundRect) pen.roundRect(box.x, box.y, box.w, box.h, 8);
    else pen.rect(box.x, box.y, box.w, box.h);
    pen.fill();
    pen.stroke();
    pen.setLineDash([]);

    pen.fillStyle = box.style.text;
    pen.font = NODE_FONT;
    pen.textAlign = 'center';
    pen.textBaseline = 'middle';
    pen.fillText(box.label, box.x + box.w / 2, box.y + box.h / 2);
  });
}

/* ------------------------------------------------------------------ *
 * Rendering - message parts
 * ------------------------------------------------------------------ */

function servicesTable(services) {
  if (!services.length) return '';
  const rows = services
    .map(
      (service) =>
        `<div class="services__row services__body">` +
        `<div><div class="services__name">${service.name}</div>` +
        (service.reasoning ? `<div class="services__reason">${service.reasoning}</div>` : '') +
        `</div><div class="services__purpose">${service.purpose}</div></div>`
    )
    .join('');

  return (
    `<div><h3 class="section-title">Recommended services</h3><div class="services">` +
    `<div class="services__row services__head">` +
    `<span class="eyebrow eyebrow--tight">Service</span>` +
    `<span class="eyebrow eyebrow--tight">Purpose</span></div>${rows}</div></div>`
  );
}

/* The pillar a note is filed under, linked to the framework's own page for it
   (F6). Both the name and the URL come from the server, which reads them off
   schema.py -- the model is never asked for a URL, so it cannot invent one. A
   note whose pillar is not one of the six arrives with no link and stays a
   plain badge. */
function pillarBadge(note, className) {
  const label = esc(note.official || note.pillar);
  if (!note.doc) return `<span class="${className}">${label}</span>`;
  return (
    `<a class="${className}" href="${esc(note.doc)}" target="_blank" rel="noopener noreferrer">` +
    `${label}</a>`
  );
}

/* How many of the six pillars this reply spoke to. Counted on the server so the
   screen and the exported document cannot disagree, and named rather than
   scored: "4 / 6" without saying which two are missing is a worse answer. */
function coverageLine(coverage) {
  if (!coverage) return '';
  const missing = coverage.missing.length
    ? ` · not addressed: ${esc(coverage.missing.join(', '))}`
    : '';
  return (
    `<span class="coverage">${coverage.covered} / ${coverage.total} pillars` +
    `${missing}</span>`
  );
}

function notesList(notes, coverage) {
  if (!notes.length) return '';
  const items = notes
    .map(
      (note) =>
        `<div class="note${note.status === 'review' ? ' note--review' : ''}">` +
        pillarBadge(note, 'pillar') +
        `<span class="note__text">${note.text}</span></div>`
    )
    .join('');
  return (
    `<div><h3 class="section-title">Well-Architected notes${coverageLine(coverage)}</h3>` +
    `<div class="notes">${items}</div></div>`
  );
}

/* ---- What it costs: one figure, one element (F1, F12) ----
 *
 * This used to be two things sitting one above the other: an orange panel with
 * the tier the model guessed, in sterling, and a card underneath with what the
 * AWS Price List said, in dollars. Two methods, two currencies, two answers, and
 * no statement of which one to quote a client.
 *
 * There is one of them now. The price list wins wherever it has a rate, the
 * advisor's own figure fills the lines it has none for, every line says which of
 * the two it is, and the band is derived from the total rather than claimed
 * beside it -- so the band and the figure cannot disagree.
 */

function money(amount) {
  const value = Number(amount) || 0;
  return `$${value.toLocaleString('en-GB', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

/** The line under the total: where, how much of it is arithmetic, on what terms. */
function estimateMeta(estimate) {
  const bits = [esc(estimate.region)];
  if (estimate.anyEstimated) {
    bits.push(
      `${estimate.linesPriced} of ${estimate.linesTotal} lines from the AWS Price List, ` +
        `the rest the advisor's estimate`
    );
  } else {
    bits.push('every line from the AWS Price List');
  }
  bits.push('on-demand list price, before discounts');
  return bits.join(' · ');
}

/* The advisor's own band held up against the arithmetic.
 *
 * This is the whole reason the model is still asked for a tier it never gets to
 * display: on its own it was a guess competing with a real figure, and
 * differenced against one it is either corroboration or a sizing bug. Two
 * half-bands apart is the threshold, computed in pricing.py so the export and
 * the CLI apply the same rule.
 */
function tierCheck(estimate) {
  if (!estimate.tierGap || estimate.tierGap < 2) return '';
  return (
    `<div class="estimate__flag">The advisor's own read of this was ` +
    `<strong>${esc(estimate.claimedTier)}</strong>; the figures put it at ` +
    `<strong>${esc(estimate.tier)}</strong>. A full band apart means something has ` +
    `been mis-sized — worth checking the quantities before quoting either.</div>`
  );
}

/* Whether the breakdown starts folded away.
 *
 * Share of dollars, not share of lines: a NAT gateway and Route 53 are two
 * estimated lines worth $70 next to one priced database line worth $500, which
 * is two thirds of the lines, an eighth of the money, and a breakdown very much
 * worth reading. Only when most of the money itself is judgement does the
 * breakdown stop being a breakdown and start being false precision.
 *
 * Strictly greater, so a dead heat opens: the default should favour showing.
 */
function mostlyEstimated(estimate) {
  return estimate.estimatedUsd > estimate.monthlyUsd / 2;
}

/* How many questions have been asked since this architecture was priced.
 *
 * Computed here rather than stored when the slot is mounted, because at mount
 * time the answer is always 0 -- the architecture has only just arrived. A prose
 * reply triggers render(), render() rebuilds this, and the marker appears with
 * no invalidation machinery of its own.
 *
 * Only the newest architecture can be stale. An earlier one that a revision
 * replaced is history, and it was right when it was written.
 */
function staleTurns(id) {
  const held = state.costs[id];
  if (!held) return 0;
  const session = activeSession();
  // A comparison has no follow-up composer, so it cannot go stale.
  if (!session || held.sessionId !== session.id) return 0;
  const latest = latestArchitecture(session);
  if (!latest || latest.index !== held.index) return 0;
  return turnsSinceArchitecture(session);
}

function staleFlag(id) {
  const behind = staleTurns(id);
  if (!behind) return '';
  return (
    `<div class="estimate__flag estimate__flag--stale">Priced before the ` +
    `${behind === 1 ? 'last answer' : `last ${behind} answers`}. Rebuild the ` +
    `architecture to reprice it.</div>`
  );
}

/** One service's figure, saying which of the three states it is in. */
function serviceAmount(service) {
  if (service.priced) return money(service.monthlyUsd);
  if (service.estimated) return `${money(service.monthlyUsd)} est.`;
  return 'no figure';
}

function serviceClass(service) {
  if (service.priced) return '';
  return service.estimated ? ' estimate__line--estimated' : ' estimate__line--unpriced';
}

function estimateBody(estimate, id) {
  if (!estimate) return '';
  if (estimate.pending) {
    return (
      `<div class="estimate__pending">${DOTS}<span class="thinking__label">` +
      `Pricing this against the AWS Price List…</span></div>`
    );
  }
  if (estimate.error) {
    return (
      `<div class="estimate__meta">${esc(estimate.error)}</div>` +
      `<button class="estimate__toggle" data-reprice="${esc(id)}">Try pricing it again</button>`
    );
  }
  if (!estimate.hasFigure) {
    return (
      `<div class="estimate__meta">Nothing in this architecture could be priced ` +
      `against the AWS Price List, and the advisor put no figure on it either, so ` +
      `there is no monthly cost to give.</div>`
    );
  }

  const open = id in state.costOpen ? state.costOpen[id] : !mostlyEstimated(estimate);
  const lines = open
    ? `<div class="estimate__lines">` +
      estimate.services
        .map(
          (service) =>
            `<div class="estimate__line${serviceClass(service)}">` +
            `<span class="estimate__name">${esc(service.name)}</span>` +
            `<span class="estimate__amount">${serviceAmount(service)}</span></div>` +
            service.lines
              .map((line) => `<div class="estimate__sub">${esc(line.detail)}</div>`)
              .join('')
        )
        .join('') +
      `</div>`
    : '';

  const detail = (state.costs[id] || {}).cost;
  return (
    `<div class="estimate__head"><span class="eyebrow eyebrow--tight">` +
    `${estimate.anyEstimated ? 'Estimated monthly cost' : 'Monthly cost'}</span>` +
    `<span class="estimate__figure">` +
    `<span class="estimate__total">${money(estimate.monthlyUsd)}</span>` +
    `<span class="estimate__band">${esc(estimate.tier)}</span></span></div>` +
    `<div class="estimate__meta">${estimateMeta(estimate)}</div>` +
    staleFlag(id) +
    tierCheck(estimate) +
    (detail && detail.detail
      ? `<div class="estimate__aside">The advisor's own read, before anything was ` +
        `looked up: ${detail.detail}</div>`
      : '') +
    `<button class="estimate__toggle" data-cost="${esc(id)}">` +
    `${open ? 'Hide' : 'Show'} the ${estimate.services.length} lines behind this</button>` +
    lines
  );
}

/** The cost element for one recommendation, or nothing until it is asked for. */
function estimateBlock(id) {
  const estimate = state.estimates[id];
  if (!estimate) return '';
  return `<div class="estimate" id="estimate-${esc(id)}">${estimateBody(estimate, id)}</div>`;
}

/** Just the figure and the band, for a comparison column with no room for more.
 *
 * No toggle here: there is nowhere to put twelve lines beside twelve more, and
 * the export is where a line-by-line comparison belongs.
 */
function estimateSummary(id) {
  const estimate = state.estimates[id];
  if (!estimate) return '';

  let body = `<span class="estimate__total estimate__total--small">…</span>`;
  // A failure and a genuinely unpriceable architecture both read "not costed",
  // but only one of them is worth offering to do again.
  if (estimate.error) {
    body =
      `<span class="estimate__amount">not costed</span>` +
      `<button class="estimate__toggle" data-reprice="${esc(id)}">Retry</button>`;
  } else if (!estimate.pending && !estimate.hasFigure) {
    body = `<span class="estimate__amount">not costed</span>`;
  } else if (!estimate.pending) {
    body =
      `<span class="estimate__figure">` +
      `<span class="estimate__total estimate__total--small">${money(estimate.monthlyUsd)}</span>` +
      `<span class="estimate__band">${esc(estimate.tier)}</span></span>` +
      `<span class="estimate__meta">${esc(estimate.region)} · ` +
      `${estimate.anyEstimated ? 'part estimated' : 'list price'}</span>`;
  }
  return `<div class="estimate estimate--compact" id="estimate-${esc(id)}">${body}</div>`;
}

/* The place an estimate goes, held open from the first render.
 *
 * The figure arrives long after the reply it belongs to, so without a slot
 * already on screen the first paint has nowhere to write and falls back to
 * render() -- which rebuilds the reply that had just been streamed into place,
 * undoing A2. An empty slot collapses, so an unpriced recommendation looks no
 * different for having one.
 */
function estimateSlot(id, compact) {
  return (
    `<div class="estimate-slot" id="estimate-slot-${esc(id)}"` +
    (compact ? ' data-compact="1"' : '') +
    `>${compact ? estimateSummary(id) : estimateBlock(id)}</div>`
  );
}

/** Repaint one estimate in place, the way A2 repaints everything else. */
function paintEstimate(id) {
  const slot = document.getElementById(`estimate-slot-${id}`);
  if (!slot) return render();
  slot.innerHTML = slot.dataset.compact ? estimateSummary(id) : estimateBlock(id);
}

/* Ask the server what this architecture costs.
 *
 * Its own request rather than part of the reply: the first architecture in a
 * region waits on AWS's own price list files, which are hundreds of megabytes,
 * and a recommendation is worth reading before that finishes.
 */
/* The document each estimate was asked for, so a failed one can be asked for
   again without going hunting for where it came from. Not part of `state`: the
   raw reply already lives in the session or the comparison, this is only a
   shortcut back to it, and snapshot() should not carry a second copy of every
   architecture into localStorage. */
const estimateSources = new Map();

async function requestEstimate(id, raw) {
  if (!raw || state.estimates[id]) return;

  estimateSources.set(id, raw);
  state.estimates[id] = { pending: true };
  paintEstimate(id);
  try {
    const payload = await request('/api/estimate', { raw });
    state.estimates[id] = payload.estimate;
  } catch (failure) {
    state.estimates[id] = { error: failure.error || 'This could not be priced.' };
  }
  paintEstimate(id);
}

/* Ask for a figure again after a failure.
 *
 * requestEstimate returns early when state.estimates[id] holds anything at all,
 * an error included, so one unreachable price list used to leave an architecture
 * unpriced for the life of the session -- and the autosave restored that state
 * on reload rather than clearing it. AWS being briefly unreachable is the
 * expected failure here, and /api/estimate spends no Anthropic money, so this is
 * offered rather than rationed. */
async function repriceEstimate(id) {
  const raw = estimateSources.get(id);
  if (!raw) return;
  delete state.estimates[id];
  await requestEstimate(id, raw);
}

/* Price every structured reply on screen that has not been priced yet, and note
 * where each one sits.
 *
 * `state.costs` is rewritten every time rather than memoised the way the estimate
 * is: the estimate never changes once it arrives, but which architecture is the
 * newest one, and how many questions have been asked since, both move as the
 * conversation goes on. That is what the staleness marker reads.
 */
function priceThread(session) {
  session.messages.forEach((message, index) => {
    if (message.role === 'assistant' && message.structured) {
      const id = `${session.id}-${index}`;
      state.costs[id] = { cost: message.cost, sessionId: session.id, index };
      const known = state.estimates[id];
      requestEstimate(id, message.raw);
      // A finished turn repaints only the reply that just streamed in (see
      // finishTurn), which leaves the architecture above it holding a figure
      // that is now a question or two out of date. Nothing about the figure
      // changed, so this is a repaint rather than a refetch, and it is what
      // makes the staleness marker appear on its own.
      if (known && !known.pending) paintEstimate(id);
    }
  });
}

/* What this architecture takes as read (F10).
 *
 * One row each, editable in place. Everything here works in raw text: the reply
 * arrives HTML-escaped from parse.py and an edit arrives decoded from an input,
 * so both are normalised to text on the way in and escaped once on the way out.
 * Mixing the two representations is the bug this function exists to not have.
 */
function assumptionText(message, id, index) {
  const staged = (state.assumptionEdits[id] || {})[index];
  if (staged !== undefined) return staged;
  return unesc((message.assumptions || [])[index]);
}

/** Every assumption on this reply, as the reader has it now. */
function assumptionsOf(message, id) {
  return (message.assumptions || []).map((_unused, index) => ({
    index,
    now: assumptionText(message, id, index),
    was: unesc(message.assumptions[index]),
  }));
}

/** The ones the reader has changed and not yet rebuilt on. */
function stagedAssumptions(message, id) {
  return assumptionsOf(message, id).filter((item) => item.now !== item.was);
}

/* Which panel on screen can be edited: the newest architecture's, and only
   while nothing is in flight.
 *
 * A thread can hold several architectures, and correcting an assumption on one
 * that has already been superseded would stage a change nothing can act on --
 * the rebuild restates the latest, not a reply four turns back. The older panels
 * are still shown, because what an architecture was sized on is part of reading
 * it; they are just read-only. The streaming reply is read-only for a plainer
 * reason: paintLive rewrites it several times a second. */
function editableAssumptionsId() {
  if (state.tab !== 'advisor' || state.loading) return '';
  const session = activeSession();
  const latest = latestArchitecture(session);
  return latest ? `${session.id}-${latest.index}` : '';
}

function assumptionRow(item, id, editable) {
  const key = `${id}:${item.index}`;
  const editing =
    state.editingAssumption &&
    state.editingAssumption.id === id &&
    state.editingAssumption.index === item.index;

  if (editing && editable) {
    return (
      `<li class="assumption assumption--editing">` +
      `<label class="sr-only" for="assumption-input">Edit this assumption</label>` +
      `<input class="assumption__input" id="assumption-input"` +
      ` data-assumption-input="${esc(key)}" maxlength="${MAX_ASSUMPTION_CHARS}"` +
      ` value="${esc(item.now)}"></li>`
    );
  }

  const changed = item.now !== item.was;
  const actions = editable
    ? `<span class="assumption__actions">` +
      `<button class="assumption__btn" data-assumption="${esc(key)}">Edit</button>` +
      (changed
        ? `<button class="assumption__btn" data-assumption-reset="${esc(key)}">Undo</button>`
        : '') +
      `</span>`
    : '';
  return (
    `<li class="assumption${changed ? ' assumption--changed' : ''}">` +
    `<span class="assumption__text">${esc(item.now)}</span>` +
    (changed ? `<span class="assumption__was">was: ${esc(item.was)}</span>` : '') +
    `${actions}</li>`
  );
}

function assumptionsBlock(message, id) {
  const items = assumptionsOf(message, id);
  if (!items.length) return '';
  const editable = id === editableAssumptionsId();
  const rows = items.map((item) => assumptionRow(item, id, editable)).join('');
  return (
    `<div class="assumptions" id="assumptions-${esc(id)}">` +
    `<div class="assumptions__head"><h3 class="section-title">What this assumes</h3>` +
    `<span class="assumptions__meta">` +
    (editable ? 'Correct one and the architecture can be rebuilt on it' : 'As it was sized') +
    `</span></div>` +
    `<ul class="assumptions__list">${rows}</ul></div>`
  );
}

/* Redraw one panel and the notice above the thread, and nothing else (A2).
 *
 * Correcting an assumption is not a reason to rebuild every message on screen,
 * and a full render would also take the caret out of the box being typed in.
 * Returns false where the nodes are not there, so the caller can fall back to
 * the whole view the way toggleSource() does. */
function paintAssumptions(id) {
  const host = document.getElementById(`assumptions-${id}`);
  const stale = $('stale-host');
  if (!host || !stale) return false;

  const session = activeSession();
  const latest = latestArchitecture(session);
  if (!latest || `${session.id}-${latest.index}` !== id) return false;

  host.outerHTML = assumptionsBlock(latest.message, id);
  stale.innerHTML = staleConstraintsNotice(session) + stagedAssumptionsNotice(session);

  const editing = document.querySelector('[data-assumption-input]');
  if (editing) {
    editing.focus();
    editing.select();
  }
  return true;
}

/* The amber notice that the architecture on screen no longer matches what the
   reader says is true, and the button that closes the gap. The same shape as the
   one the region selectors raise (F5), and for the same reason: nothing is
   blocked, the gap is stated, and one deliberate press spends the call. */
function stagedAssumptionsNotice(session) {
  const latest = latestArchitecture(session);
  if (!latest) return '';
  const id = `${session.id}-${latest.index}`;
  const staged = stagedAssumptions(latest.message, id);
  if (!staged.length) return '';
  const count = staged.length === 1 ? 'One assumption has' : `${staged.length} assumptions have`;
  return (
    `<div class="stale">` +
    `<span class="stale__mark" aria-hidden="true">!</span>` +
    `<div class="stale__text">` +
    `<strong>${count} been corrected.</strong> The architecture below was sized on the ` +
    `originals, so the services and the prices still answer to them. ` +
    `<strong>Rebuild on these assumptions</strong> restates it against what you have ` +
    `written, in one review however many you changed.` +
    `</div>` +
    `<button class="btn btn--primary btn--sm" data-rebuild-assumptions="1"` +
    `${state.loading ? ' disabled' : ''}>Rebuild on these</button></div>`
  );
}

function diagramBlock(message, id) {
  const diagram = message.diagram;
  if (!diagram || !diagram.source) return '';

  // Kept aside so the source can be shown, and the files written, without
  // re-rendering the message they belong to. See toggleSource() and
  // downloadDiagramSvg().
  state.sources[id] = diagram.source;
  state.diagrams[id] = diagram;
  state.diagramTitles[id] = message.headline || '';

  const svg = diagramSvg(diagram);
  const open = !!state.showSource[id];
  const source = open
    ? `<div class="source"><div class="source__head">Diagram source</div>` +
      `<pre>${esc(diagram.source)}</pre></div>`
    : '';

  // The caption and the two downloads share the right-hand end of the head. A
  // diagram that could not be drawn has nothing to write out, so it keeps the
  // caption alone.
  const meta = `<span class="diagram__meta">Rendered from Mermaid · flowchart LR</span>`;
  const actions = svg
    ? `<span class="diagram__actions">${meta}` +
      `<button class="diagram__file" data-download-svg="${esc(id)}">SVG</button>` +
      `<button class="diagram__file" data-download-png="${esc(id)}">PNG</button></span>`
    : meta;

  return (
    `<div><div class="diagram__head"><h3 class="section-title">Architecture diagram</h3>` +
    `${actions}</div>` +
    (svg ? `<div class="diagram">${svg}</div>` : '') +
    `<button class="diagram__toggle" data-source="${esc(id)}">` +
    `${open ? 'Hide' : 'View'} diagram source</button>${source}</div>`
  );
}

/** `anchor` overrides the element id, so the context strip has a stable target. */
function recommendation(message, id, anchor) {
  const head =
    `<div class="rec__head"><span class="eyebrow">AWS architecture recommendation</span>` +
    (message.headline ? `<h2 class="rec__title">${esc(message.headline)}</h2>` : '') +
    (message.overview ? `<div class="rec__overview prose">${message.overview}</div>` : '') +
    `</div>`;

  // The notes stand alone in the rail now. The cost used to sit under them as an
  // orange panel; it is one element below the grid instead, because a figure with
  // a breakdown under it needs the full measure.
  const side = notesList(message.notes, message.coverage);
  const grid =
    message.services.length && side
      ? `<div class="rec__grid">${servicesTable(message.services)}<div class="rec__side">${side}</div></div>`
      : servicesTable(message.services) + (side ? `<div class="rec__side">${side}</div>` : '');

  const extra = message.prose ? `<div class="prose">${message.prose}</div>` : '';
  return (
    `<div class="rec" id="rec-${esc(anchor || id)}">` +
    `${head}${assumptionsBlock(message, id)}${grid}${estimateSlot(id, false)}` +
    `${extra}${diagramBlock(message, id)}</div>`
  );
}

function userMessage(content) {
  return (
    `<div class="msg"><div class="avatar msg__avatar">${USER_MARK}</div>` +
    `<div class="bubble">${esc(content)}</div></div>`
  );
}

function replyBody(message, id, anchor) {
  // A follow-up is prose, but it can still come back with a revised diagram.
  return message.structured
    ? recommendation(message, id, anchor)
    : `<div class="prose">${message.prose || esc(message.raw || '')}</div>` +
        diagramBlock(message, id);
}

function assistantMessage(message, id, anchor) {
  return (
    `<div class="msg"><img class="msg__mark" src="assets/mark-placeholder.svg" alt="">` +
    `<div class="reply">${replyBody(message, id, anchor)}</div></div>`
  );
}

const DOTS =
  `<span class="thinking__dot"></span><span class="thinking__dot"></span>` +
  `<span class="thinking__dot"></span>`;

/** The waiting state: three dots, and whatever Claude is currently weighing up. */
function thinkingBlock(line) {
  return (
    `<div class="thinking">${DOTS}<span class="thinking__label">Thinking…</span>` +
    (line ? `<span class="thinking__note">${esc(line)}</span>` : '') +
    `</div>`
  );
}

/** The reply being written right now, drawn from whatever has arrived so far. */
function liveBody() {
  const live = state.streaming;
  if (!live) return thinkingBlock('');
  return live.message ? replyBody(live.message, 'live') : thinkingBlock(live.thinking);
}

function liveMessage() {
  return (
    `<div class="msg"><img class="msg__mark" src="assets/mark-placeholder.svg" alt="">` +
    `<div class="reply" id="live-reply">${liveBody()}</div></div>`
  );
}

/* Repaint just the reply being streamed.
 *
 * A partial arrives several times a second, and render() rebuilds the whole
 * view, which at that rate is both wasteful and visibly jumpy. Everything else
 * on screen is unchanged while a reply streams, so only this one node is
 * touched; the full render is the fallback for when it is not on screen yet.
 */
function paintLive() {
  const node = $('live-reply');
  if (node) node.innerHTML = liveBody();
  else render();
}

// The app has no idea who you are, and asking the server would mean it telling
// every client the machine's account name. The bubble avatar is there to mark
// your turn in the thread, not to identify you.
const USER_MARK = '·';

/* ------------------------------------------------------------------ *
 * Rendering - alerts
 * ------------------------------------------------------------------ */

const ALERT_TONES = {
  missing_key: 'danger',
  api: 'danger',
  error: 'danger',
  timeout: 'warning',
  connection: 'warning',
  truncated: 'warning',
  too_long: 'warning',
  refusal: 'warning',
  rate_limit: 'warning',
};

function alertBlock(alert) {
  if (!alert) return '';
  const tone = ALERT_TONES[alert.kind] || 'neutral';

  const actions = (alert.actions || [])
    .map(
      (action) =>
        `<button class="btn btn--alert btn--${action.tone || 'dark'}" data-action="${esc(action.id)}">` +
        `${esc(action.label)}</button>`
    )
    .join('');

  // The README panel belongs to the missing-key alert only.
  const setup =
    alert.kind === 'missing_key' && state.setupHtml
      ? `<div class="setup prose">${state.setupHtml}</div>`
      : '';

  return (
    `<div class="alerts"><div class="alert alert--${tone}"><div class="alert__bar"></div>` +
    `<div class="alert__body"><p class="alert__title">${esc(alert.title)}</p>` +
    `<p class="alert__text">${alert.text}</p>` +
    (actions ? `<div class="alert__actions">${actions}</div>` : '') +
    setup +
    `</div></div></div>`
  );
}

function keyAlert() {
  return {
    kind: 'missing_key',
    title: 'API key missing',
    text:
      '<code>ANTHROPIC_API_KEY</code> is not set. Add it to your <code>.env</code> file or ' +
      'environment, then reload.',
    actions: [
      { id: 'recheck', label: 'Check again', tone: 'dark' },
      { id: 'guide', label: 'Setup guide', tone: 'primary' },
    ],
  };
}

const ALERT_TITLES = {
  truncated: 'That answer was cut short',
  too_long: 'That is too much to send at once',
  refusal: 'Claude declined that one',
  // A follow-up that searched the web and never came back from it (F2).
  paused: 'That search did not finish',
  // The server has already retried this several times, and the message carries
  // the window the API asked for, so all this needs to do is name it.
  rate_limit: 'Rate limited',
};

function errorAlert(failure) {
  if (failure.kind === 'missing_key') return keyAlert();
  if (failure.kind === 'timeout') {
    return {
      kind: 'timeout',
      title: 'The request timed out',
      text:
        'Claude took longer than two minutes to respond. Your workload description has ' +
        'been kept — try again, or shorten it if the problem persists.',
      actions: [
        { id: 'retry', label: 'Try again', tone: 'primary' },
        { id: 'edit', label: 'Edit the description', tone: 'dark' },
      ],
    };
  }
  if (ALERT_TITLES[failure.kind]) {
    return {
      kind: failure.kind,
      title: ALERT_TITLES[failure.kind],
      text: esc(failure.error),
      actions: [
        { id: 'retry', label: 'Try again', tone: 'primary' },
        { id: 'edit', label: 'Edit the question', tone: 'dark' },
      ],
    };
  }
  return {
    kind: failure.kind || 'error',
    title: 'That request did not go through',
    text: esc(failure.error),
    actions: [{ id: 'retry', label: 'Try again', tone: 'primary' }],
  };
}

/* A passing message in the corner. `label` names it and `action` is the button
   beside it; an export that worked has nothing to retry, so it passes null. */
function toast(message, { label = 'Toast', action = 'Retry' } = {}) {
  const node = document.createElement('div');
  node.className = 'toast';
  node.innerHTML =
    `<span class="eyebrow eyebrow--primary eyebrow--tight">${esc(label)}</span>` +
    `<span class="toast__text">${esc(message)}</span>` +
    (action ? `<button class="toast__action">${esc(action)}</button>` : '');
  const button = node.querySelector('.toast__action');
  if (button) {
    button.addEventListener('click', () => {
      node.remove();
      if (state.pending) retry();
    });
  }
  dom.toasts.appendChild(node);
  setTimeout(() => node.remove(), 12000);
}

/* ------------------------------------------------------------------ *
 * Rendering - views
 * ------------------------------------------------------------------ */

const PENCIL_ICON =
  `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" ` +
  `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">` +
  `<path d="M11.3 2.2a1.2 1.2 0 0 1 1.7 0l.8.8a1.2 1.2 0 0 1 0 1.7l-7 7-3 .8.8-3z"/></svg>`;

const DOWNLOAD_ICON =
  `<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none" ` +
  `stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">` +
  `<path d="M8 2v8m0 0 3-3m-3 3L5 7M3 12.5h10"/></svg>`;

const BIN_ICON =
  `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" ` +
  `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">` +
  `<path d="M2.8 4.5h10.4M6.2 4.5V3.1a.9.9 0 0 1 .9-.9h1.8a.9.9 0 0 1 .9.9v1.4"/>` +
  `<path d="M4.1 4.5l.6 8.3a1 1 0 0 0 1 .9h4.6a1 1 0 0 0 1-.9l.6-8.3"/>` +
  `<path d="M6.7 7v4M9.3 7v4"/></svg>`;

const TAG_ICON =
  `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" ` +
  `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">` +
  `<path d="M7.4 2.4H3a.6.6 0 0 0-.6.6v4.4a.6.6 0 0 0 .18.42l6 6a.6.6 0 0 0 .84 0l4.2-4.2` +
  `a.6.6 0 0 0 0-.84l-6-6a.6.6 0 0 0-.42-.18z"/><path d="M5.1 5.1h.01"/></svg>`;

const NOTHING_SAVED_HTML =
  `<p class="sidebar__empty">Nothing saved yet. Use the button below to keep a ` +
  `conversation.</p>`;

/* A different sentence from "nothing saved yet": one says to go and make
   something, the other says to widen the search. */
const NOTHING_MATCHED_HTML =
  `<p class="sidebar__empty">Nothing matched. <button class="mini" ` +
  `data-clear-filters="1">Clear the filters</button></p>`;

const NOTHING_OPEN_HTML =
  `<p class="sidebar__empty">Nothing open yet. A conversation here survives a reload; ` +
  `save one to keep it for good.</p>`;

function savedItem(item) {
  if (state.renaming === item.id) {
    return (
      `<div class="saved-item"><input class="rename" data-rename-input="${item.id}" ` +
      `value="${esc(item.title)}" maxlength="80" aria-label="Rename conversation"></div>`
    );
  }

  if (state.deleting === item.id) {
    return (
      `<div class="saved-item"><div class="confirm">` +
      `<div class="confirm__text">Delete “${esc(item.title)}”?</div>` +
      `<div class="confirm__actions">` +
      `<button class="mini mini--danger" data-delete-yes="${item.id}">Delete</button>` +
      `<button class="mini" data-delete-no="1">Cancel</button>` +
      `</div></div></div>`
    );
  }

  const badge = item.mode === 'compare' ? `<span class="session__badge">Compare</span>` : '';
  const meta = [savedWhen(item), plural(item.messageCount, 'message')]
    .filter(Boolean)
    .join(' · ');

  return (
    `<div class="saved-item">` +
    `<button class="session${item.id === state.openedId ? ' is-active' : ''}" ` +
    `data-open="${item.id}" title="Open this saved conversation">` +
    `<div class="session__title">${esc(item.title)}${badge}</div>` +
    `<div class="session__meta"><span>${esc(meta)}</span>` +
    `${costChip(item.usage)}</div></button>` +
    `<div class="saved-item__tools">` +
    `<a class="icon-btn" href="/api/conversations/${item.id}/export" ` +
    `title="Download as JSON" aria-label="Download ${esc(item.title)} as JSON">${DOWNLOAD_ICON}</a>` +
    `<button class="icon-btn" data-tag="${item.id}" ` +
    `title="Add a tag" aria-label="Tag ${esc(item.title)}">${TAG_ICON}</button>` +
    `<button class="icon-btn" data-rename="${item.id}" ` +
    `title="Rename" aria-label="Rename ${esc(item.title)}">${PENCIL_ICON}</button>` +
    `<button class="icon-btn icon-btn--danger" data-delete="${item.id}" ` +
    `title="Delete" aria-label="Delete ${esc(item.title)}">${BIN_ICON}</button>` +
    `</div>` +
    tagRow(item) +
    `</div>`
  );
}

/* A conversation's tags, each removable, and the input while one is being added.
   Tags are what somebody called this conversation, as against the region and
   cost band read off the reply -- those are filters, not labels. */
function tagRow(item) {
  const tags = (item.tags || [])
    .map(
      (tag) =>
        `<button class="tag" data-untag="${item.id}" data-tag-name="${esc(tag)}" ` +
        `title="Remove this tag">${esc(tag)}<span class="tag__x">×</span></button>`
    )
    .join('');
  const adding =
    state.tagging === item.id
      ? `<input class="rename tag-input" data-tag-input="${item.id}" maxlength="30" ` +
        `placeholder="Tag, then Enter" aria-label="New tag">`
      : '';
  if (!tags && !adding) return '';
  return `<div class="tags">${tags}${adding}</div>`;
}

/* The cost bands and tags a saved conversation can be narrowed by (F8).
   Only the bands that something is actually filed under, so the row does not
   offer five filters where four of them find nothing. */
function renderFilters() {
  if (!dom.savedFilters) return;
  const present = new Set(state.saved.map((item) => item.tier).filter(Boolean));
  const bands = state.query.tier ? new Set([...present, state.query.tier]) : present;

  const chip = (label, field, value) =>
    `<button class="filter${state.query[field] === value ? ' is-on' : ''}" ` +
    `data-filter-${field}="${esc(value)}">${esc(label)}</button>`;

  const parts = TIERS.filter((tier) => bands.has(tier)).map((tier) => chip(tier, 'tier', tier));
  state.tags.forEach((tag) => parts.push(chip(`#${tag}`, 'tag', tag)));
  if (activeFilters()) {
    parts.push(`<button class="filter filter--clear" data-clear-filters="1">Clear</button>`);
  }
  dom.savedFilters.innerHTML = parts.join('');
}

function renderSavedList() {
  renderFilters();
  dom.savedList.innerHTML = state.saved.length
    ? state.saved.map(savedItem).join('')
    : state.searching
      ? NOTHING_MATCHED_HTML
      : NOTHING_SAVED_HTML;

  const input =
    dom.savedList.querySelector('[data-rename-input]') ||
    dom.savedList.querySelector('[data-tag-input]');
  if (input) {
    input.focus();
    input.select();
  }
}

/** What a conversation cost, for the right-hand end of its row in the sidebar. */
function costChip(usage) {
  if (!usage || !usage.calls) return '';
  return (
    `<span class="session__cost" title="${esc(usageDetail(usage))}">` +
    `${esc(costLabel(usage))}</span>`
  );
}

/** One session in the sidebar: what it is, when, and what it has cost. */
function sessionRow(session, active) {
  const when = whenLabel(session.updatedAt || session.createdAt);
  const cost = costChip(session.usage);
  return (
    `<button class="session${active ? ' is-active' : ''}" data-session="${esc(session.id)}">` +
    `<div class="session__title">${esc(session.title || 'Untitled workload')}` +
    (isUnsaved(session) ? `<span class="session__dot" title="Not saved">•</span>` : '') +
    `</div>` +
    `<div class="session__meta"><span>${esc(when)} · ` +
    `${esc(plural(session.messages.length, 'message'))}</span>${cost}</div></button>`
  );
}

function renderSidebar() {
  // Exactly one row: the conversation being worked in. A reopened saved one is
  // shown as active in the Saved list instead, and clicking this row comes back.
  const live = liveSession();
  dom.sessions.innerHTML = live
    ? sessionRow(live, live.id === state.activeId)
    : NOTHING_OPEN_HTML;

  const session = activeSession();
  const hasContent = session.messages.length || (state.compare.results && state.tab === 'compare');
  dom.saveBtn.disabled = !hasContent || state.loading;
  // F3: there is nothing to hand over until a reply has rendered.
  dom.exportBtn.disabled = !exportable() || state.loading || state.compare.loading;

  dom.savedBox.hidden = !state.savedNote;
  if (state.savedNote) dom.savedPath.textContent = state.savedNote;
}

/** What a conversation has cost, spelled out behind the figure on its row.
 *
 * The figure is on the session row, where it belongs: a running total per
 * conversation is the useful one, and the single cross-session total this
 * replaced never was. This is the breakdown, on the row's tooltip.
 */
function usageDetail(usage) {
  const cached = usage.cacheReadTokens ? ` · ${tokens(usage.cacheReadTokens)} cached` : '';
  // Searches are billed per request, so they are counted separately from tokens.
  // plural() adds an s, which is the wrong one for "search".
  const searched = usage.searches
    ? ` · ${tokens(usage.searches)} web ${usage.searches === 1 ? 'search' : 'searches'}`
    : '';

  return (
    `${plural(usage.calls, 'API call')} · ${tokens(usage.inputTokens)} in / ` +
    `${tokens(usage.outputTokens)} out tokens${cached}${searched} · list price, USD`
  );
}

function contextStrip(session) {
  const first = session.messages.find((m) => m.role === 'assistant' && m.structured);
  const userTurns = session.messages.reduce((n, m) => n + (m.role === 'user' ? 1 : 0), 0);
  if (!first || userTurns < 2) return '';

  const bits = [];
  if (first.services && first.services.length) bits.push(plural(first.services.length, 'service'));
  if (first.cost && first.cost.tier)
    bits.push(`cost tier <strong>${esc(first.cost.tier)}</strong>`);

  const label = session.title || 'This conversation';

  return (
    `<div class="context"><span class="eyebrow eyebrow--tight">In context</span>` +
    `<span class="context__text">${esc(label)}${bits.length ? ` — ${bits.join(', ')}` : ''}</span>` +
    `<a class="context__link" href="#rec-${esc(session.id)}-0" data-jump="1">View full recommendation</a></div>`
  );
}

const WELCOME_HTML =
  `<div class="welcome"><span class="eyebrow eyebrow--primary">Architecture Advisor</span>` +
  `<h1 class="welcome__title">Secure foundations for <em>bold innovation</em>.</h1>` +
  `<p class="welcome__lead">Describe a workload in plain English. Get a reviewed AWS ` +
  `architecture, Well-Architected notes and what it costs to run — in under a minute.</p>` +
  `<div class="welcome__stats">` +
  `<div><div class="welcome__stat-value">6</div>` +
  `<div class="welcome__stat-label">Well-Architected pillars checked</div></div>` +
  `<div><div class="welcome__stat-value">$</div>` +
  `<div class="welcome__stat-label">Priced against the AWS Price List</div></div>` +
  `</div></div>` +
  `<div class="examples"><span class="eyebrow">Try one of these</span>` +
  EXAMPLES.map((example, i) => `<button class="example" data-example="${i}">${esc(example)}</button>`).join('') +
  `</div>`;

/* The chips under a reply (F13).
 *
 * A function of the architecture on screen rather than a constant, because the
 * three generic questions were the same whatever was asked, and the chips are
 * the one place this app can teach a reader who does not know what to ask next.
 *
 * The text rides on the attribute rather than an index into a list, so what is
 * sent is exactly what the button says. Server-side, parse.py has already
 * escaped them, capped them at three and dropped anything asking for a rebuild.
 * The fallback is the old hardcoded three: an empty row where the guidance
 * should be is worse than a generic one. */
function suggestionsHtml(session) {
  const architecture = latestArchitecture(session);
  const offered = (architecture && architecture.message.nextQuestions) || [];
  const chips = offered.length ? offered : SUGGESTIONS.map((chip) => chip.text);
  return (
    `<div class="suggestions">` +
    chips.map((text) => `<button class="suggestion" data-suggestion="${text}">${text}</button>`).join('') +
    `</div>`
  );
}

/* Rebuilding the architecture, given the weight it actually carries: it costs a
   full structured turn and it replaces the recommendation on screen. The accent and
   on its own row, above the cheap questions, so it does not read as a fourth
   equally casual prompt. */
const REBUILD_NOTE =
  'Rewrites the recommendation using everything agreed since — sizes, services and the ' +
  'diagram. One full review.';

/* What the architecture on screen was actually built for, against what the
 * selectors say now.
 *
 * The two can disagree, because a follow-up is a prose turn and cannot change an
 * architecture: moving the region at turn five changes no service and no price
 * until the architecture is asked for again. Rather than pretend otherwise, or
 * lock the selectors and make somebody start over, the gap is stated and the
 * Rebuild button is what closes it.
 *
 * Nothing is blocked. A reader may well want to ask a question before rebuilding,
 * and refusing to answer it would be the app deciding it knows better. */
function constraintsDiffer(session) {
  const architecture = latestArchitecture(session);
  const built = architecture && architecture.message.builtFor;
  if (!built) return null;

  const now = { region: state.constraints.region, compliance: liveCompliance() };
  if (built.region === now.region && built.compliance === now.compliance) return null;
  return { built, now };
}

/** The compliance profile as it is stored: "none" and "" are the same answer. */
function liveCompliance() {
  return state.constraints.compliance === 'none' ? '' : state.constraints.compliance;
}

function constraintLabel(constraints) {
  const region = REGIONS.find((item) => item.id === constraints.region);
  const regime = COMPLIANCE.find((item) => item.id === (constraints.compliance || 'none'));
  const parts = [];
  if (region && region.id) parts.push(region.label);
  if (regime && regime.id !== 'none') parts.push(regime.label);
  return parts.length ? parts.join(' · ') : 'no region or regime set';
}

function staleConstraintsNotice(session) {
  const gap = constraintsDiffer(session);
  if (!gap) return '';
  return (
    `<div class="stale">` +
    `<span class="stale__mark" aria-hidden="true">!</span>` +
    `<div class="stale__text">` +
    `<strong>Rebuild before you carry on.</strong> The architecture below was built for ` +
    `${esc(constraintLabel(gap.built))}, and you have changed that to ` +
    `${esc(constraintLabel(gap.now))}. A follow-up question cannot move it — only ` +
    `<strong>Rebuild the architecture</strong> will, and the prices go with it.` +
    `</div>` +
    `<button class="btn btn--primary btn--sm" data-rebuild="1" title="${esc(REBUILD_NOTE)}">` +
    `Rebuild now</button></div>`
  );
}

/* Rebuilding the architecture, given the weight it actually carries: a full
   structured turn that replaces the recommendation on screen.
 *
 * One word, in the composer row and to the left of the box you type in, so the
 * question sits between Rebuild and Send. It had a row of its own with a
 * sentence beside it, which left a band of empty space under the chips and
 * nothing in it. The sentence is the tooltip now; the weight is carried by it
 * being a button in the primary colour rather than a chip. */
function rebuildButton(session) {
  if (!latestArchitecture(session)) return '';
  const stopped = state.health.ok === false || state.loading ? ' disabled' : '';
  return (
    `<button class="btn btn--primary btn--lg composer__rebuild" data-rebuild="1"` +
    ` title="${esc(REBUILD_NOTE)}"${stopped}>Rebuild</button>`
  );
}

function renderAdvisor() {
  const session = activeSession();
  const messages = session.messages;
  const empty = !messages.length;

  // The first structured reply is the anchor the context strip jumps to.
  const firstStructured = messages.findIndex((m) => m.role === 'assistant' && m.structured);
  const thread = messages
    .map((message, index) =>
      message.role === 'user'
        ? userMessage(message.content)
        : assistantMessage(
            message,
            `${session.id}-${index}`,
            index === firstStructured ? `${session.id}-0` : null
          )
    )
    .join('');

  const last = messages[messages.length - 1];
  const settled = !state.loading && !state.alert && last && last.role === 'assistant';
  // Nothing to rebuild until there is an architecture to rebuild.
  const suggestions = settled ? suggestionsHtml(session) : '';

  const blocked = state.health.ok === false;
  const stopped = blocked || state.loading ? ' disabled' : '';
  const placeholder = empty
    ? 'Describe your workload, or ask a follow-up question…'
    : 'Ask a follow-up question, or describe another workload…';

  dom.advisorView.innerHTML =
    (empty ? WELCOME_HTML : `<div id="context-host">${contextStrip(session)}</div>`) +
    // At the top of the conversation, not above the composer. They are settled
    // once and then read; keeping them by the composer put a pair of selects
    // between the follow-up chips and the box you type in, which is the
    // busiest part of the screen and the one place they are least often
    // wanted. Changing one now means scrolling back to the brief they belong
    // to, which is the right amount of friction for a decision that needs a
    // rebuild to take effect.
    constraintRow('advisor') +
    `<div id="stale-host">${staleConstraintsNotice(session)}` +
    `${stagedAssumptionsNotice(session)}</div>` +
    (thread
      ? `<div class="thread" id="thread">${thread}${state.loading ? liveMessage() : ''}</div>`
      : '') +
    `<div id="suggestions">${suggestions}</div>` +
    alertBlock(state.alert) +
    `<div class="composer">` +
    rebuildButton(session) +
    `<label class="sr-only" for="composer-input">Describe your workload, or ask a follow-up</label>` +
    `<input class="composer__input" id="composer-input" autocomplete="off"` +
    ` placeholder="${placeholder}"${stopped}>` +
    `<button class="btn btn--primary btn--lg" id="send-btn"${stopped}>Send</button></div>`;

  // The thread is rebuilt on every render, so an open assumption editor is a
  // fresh element each time and has to be given the caret back -- the same thing
  // renderSavedList does for a rename box, and for the same reason.
  const editing = dom.advisorView.querySelector('[data-assumption-input]');
  if (editing) {
    editing.focus();
    editing.select();
  }
}

/* ---- What the architecture has to be built for (F5) ---- */

/* The two selectors, above the composer and above the compare inputs.
 *
 * The first `<select>` elements in the app, so they are wired by a `change`
 * listener rather than the delegated click handler: the CSP forbids inline
 * handlers, and a select does not fire click when the keyboard moves it. */
function constraintRow(where) {
  const off = state.health.ok === false || state.loading || state.compare.loading ? ' disabled' : '';
  const option = (item, chosen) =>
    `<option value="${esc(item.id)}"${item.id === chosen ? ' selected' : ''}>${esc(item.label)}</option>`;
  return (
    `<div class="constraints">` +
    `<label class="sr-only" for="region-${where}">Region</label>` +
    `<select class="constraints__select" id="region-${where}" data-constraint="region"${off}>` +
    REGIONS.map((item) => option(item, state.constraints.region)).join('') +
    `</select>` +
    `<label class="sr-only" for="compliance-${where}">Compliance</label>` +
    `<select class="constraints__select" id="compliance-${where}" data-constraint="compliance"${off}>` +
    COMPLIANCE.map((item) => option(item, state.constraints.compliance || 'none')).join('') +
    `</select></div>`
  );
}

/* Only the parts that mean something. The server ignores the rest, but a body
   that says `compliance: "none"` on every turn reads as a choice nobody made. */
function constraintsBody() {
  const body = {};
  if (state.constraints.region) body.region = state.constraints.region;
  if (state.constraints.compliance && state.constraints.compliance !== 'none') {
    body.compliance = state.constraints.compliance;
  }
  return body;
}

/* ---- Comparing more than two (F9) ---- */

function addWorkload() {
  if (state.compare.workloads.length >= MAX_COMPARE) return;
  state.compare.workloads = readWorkloads().concat('');
  state.compare.results = null; // a new column makes the old comparison partial
  state.compare.thinking = state.compare.workloads.map(() => '');
  render();
}

function dropWorkload(index) {
  if (state.compare.workloads.length <= MIN_COMPARE) return;
  const kept = readWorkloads();
  kept.splice(index, 1);
  state.compare.workloads = kept;
  state.compare.results = null;
  state.compare.thinking = kept.map(() => '');
  render();
}

/* ---- Finding a saved conversation (F8) ---- */

let searchTimer = null;

/* Typing waits; picking a filter does not.
 *
 * The debounce is here rather than a rate limit on the server, because a
 * search-as-you-type box firing per keystroke is a client problem with a client
 * fix, and /api/search costs no money to serve. */
function scheduleSearch() {
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    searchTimer = null;
    refreshSaved();
  }, 250);
}

function activeFilters() {
  const { text, tier, service, tag } = state.query;
  return Boolean(text.trim() || tier || service || tag);
}

function setFilter(field, value) {
  // Clicking the filter that is already on takes it off again, which is what a
  // chip that looks pressed should do.
  state.query[field] = state.query[field] === value ? '' : value;
  render();
  refreshSaved();
}

function clearFilters() {
  state.query = { text: '', tier: '', service: '', tag: '' };
  const box = $('saved-search');
  if (box) box.value = '';
  render();
  refreshSaved();
}

function startTagging(conversationId) {
  state.tagging = state.tagging === conversationId ? null : conversationId;
  render();
}

async function commitTag(conversationId, text) {
  const tag = (text || '').trim();
  state.tagging = null;
  if (!tag) return render();
  try {
    await request(`/api/conversations/${conversationId}/tags`, { tag });
  } catch (failure) {
    toast(failure.error || 'Could not add that tag.');
  }
  await refreshSaved();
  render();
}

async function untag(conversationId, tag) {
  try {
    await request(`/api/conversations/${conversationId}/tags`, { tag, remove: true });
  } catch (failure) {
    toast(failure.error || 'Could not remove that tag.');
  }
  await refreshSaved();
  render();
}

/** "Option A", "Option B", ... for however many columns there are (F9). */
function optionLabel(index) {
  return `Option ${String.fromCharCode(65 + index)}`;
}

function optionColumn(result, index, workload) {
  const badge = optionLabel(index);
  // Which grid column this one occupies. Set inline because `.option` is
  // `display: contents`, so there is no box to target and the number of columns
  // is not known until the reply arrives.
  const at = ` style="--option-column:${index + 1}"`;
  if (!result) {
    // Still being written. The five children below are kept, empty, so this
    // column keeps sharing the grid's rows with the ones beside it.
    return (
      `<div class="option"${at}>` +
      `<div class="option__head"><span class="option__badge">${badge}</span></div>` +
      thinkingBlock(state.compare.thinking[index]) +
      `<div></div><div></div><div></div><div></div></div>`
    );
  }
  // Conversations saved before headlines existed fall back to their workload.
  const name = result.headline || (workload || '').slice(0, 34) || 'Recommendation';
  const stack = result.services.length
    ? `<div class="stack">` +
      result.services
        .map(
          (service) =>
            `<div class="stack__row"><span class="stack__name">${service.name}</span>` +
            `<span class="stack__purpose">${service.purpose}</span></div>`
        )
        .join('') +
      `</div>`
    : '';

  const notes = result.notes.length
    ? `<div class="note-lines">` +
      result.notes
        .slice(0, 4)
        .map(
          (note) =>
            `<div class="note-line${note.status === 'review' ? ' note-line--review' : ''}">` +
            pillarBadge(note, 'note-line__pillar') +
            `<span class="note-line__text">${note.text}</span></div>`
        )
        .join('') +
      `</div>`
    : '';

  // Always five children, empty where a section is absent, so the two columns
  // keep sharing the grid's rows. The cost used to be two of them -- the tier
  // inline and the priced figure below it -- and is now one (F12).
  return (
    `<div class="option"${at}>` +
    `<div class="option__head"><span class="option__badge">${badge}</span>` +
    `<span class="option__name">${esc(name)}</span></div>` +
    (result.overview ? `<div class="option__summary prose">${result.overview}</div>` : '<div></div>') +
    (stack || '<div></div>') +
    (notes || '<div></div>') +
    estimateSlot(`compare-${index}`, true) +
    `</div>`
  );
}

const COMPARE_INTRO_HTML =
  `<div><h2 class="compare__title">Compare workloads</h2>` +
  `<p class="compare__lead">Describe two to four, and the advisor returns each architecture ` +
  `side by side so you can weigh the trade-offs before committing.</p></div>`;

function renderCompare() {
  const { workloads, results, error, loading } = state.compare;
  const blocked = state.health.ok === false;
  const off = blocked ? ' disabled' : '';

  const emptyAlert = error
    ? alertBlock({
        kind: error.kind === 'empty' ? 'empty' : error.kind,
        title: error.kind === 'empty' ? 'Nothing to compare yet' : 'That comparison did not run',
        text: esc(error.error),
        actions:
          error.kind === 'empty' ? [] : [{ id: 'recompare', label: 'Try again', tone: 'primary' }],
      })
    : '';

  const fields = workloads
    .map(
      (text, index) =>
        `<div class="field"><label class="eyebrow eyebrow--tight" for="workload-${index}">` +
        `Workload ${String.fromCharCode(65 + index)}` +
        (index >= MIN_COMPARE
          ? ` <button class="mini mini--danger" data-drop-workload="${index}" ` +
            `title="Remove this workload">Remove</button>`
          : '') +
        `</label><textarea id="workload-${index}" ` +
        `placeholder="Describe workload ${String.fromCharCode(65 + index)}…"${off}>` +
        `${esc(text)}</textarea></div>`
    )
    .join('');

  const room = workloads.length < MAX_COMPARE;

  dom.compareView.innerHTML =
    COMPARE_INTRO_HTML +
    constraintRow('compare') +
    `<div class="compare__inputs compare__inputs--${workloads.length}">${fields}</div>` +
    `<div class="compare__actions">` +
    (room
      ? `<button class="btn btn--ghost btn--md" id="add-workload"${off}>Add a workload</button>`
      : '') +
    `<button class="btn btn--primary btn--md" id="compare-btn"${
      blocked || loading ? ' disabled' : ''
    }>${loading ? 'Comparing…' : 'Compare architectures'}</button></div>` +
    (blocked ? alertBlock(keyAlert()) : '') +
    emptyAlert +
    (results
      ? `<div class="rule"></div>` +
        `<div class="compare__scroll"><div class="compare__results compare__results--${
          results.length
        }" id="compare-results">${compareColumns()}</div></div>`
      : '');
}

function compareColumns() {
  const { workloads, results } = state.compare;
  return (results || [])
    .map((result, index) => optionColumn(result, index, workloads[index] || ''))
    .join('');
}

/** Repaint just the two columns while they stream. See paintLive(). */
function paintCompare() {
  const node = $('compare-results');
  if (node) node.innerHTML = compareColumns();
  else render();
}

/** Rebuild the visible view wholesale. The fallback for everything above. */
function render() {
  // The view is rebuilt from scratch, so carry a half-typed message across.
  const composer = $('composer-input');
  const draft = composer ? composer.value : '';

  renderChrome();
  // Only the view on screen is rebuilt; the other one is rendered when its tab
  // is next shown, which always goes through render() again.
  if (state.tab === 'advisor') renderAdvisor();
  else renderCompare();

  const restored = $('composer-input');
  if (restored && draft && !restored.value) restored.value = draft;
}

/* ------------------------------------------------------------------ *
 * Targeted updates (A2)
 *
 * render() rebuilds the whole visible view, which is fine when the view
 * genuinely changed and wasteful when one message was appended to the end of a
 * long thread: every node is destroyed and rebuilt, the browser loses anything
 * it was holding onto in them, and nothing can be animated.
 *
 * The three things that happen most often are done in place instead: adding a
 * turn, finishing one, and showing the diagram source. Each checks that the
 * nodes it expects are actually on screen, and returns false if they are not,
 * so the wholesale rebuild is always there as the fallback.
 * ------------------------------------------------------------------ */

/** Everything outside the two views: the sidebar, the cost, the tabs. */
function renderChrome() {
  document.body.classList.toggle('is-busy', state.loading || state.compare.loading);
  dom.modelLabel.textContent = state.health.model || DEFAULT_MODEL;

  const onAdvisor = state.tab === 'advisor';
  dom.tabAdvisor.classList.toggle('is-active', onAdvisor);
  dom.tabCompare.classList.toggle('is-active', !onAdvisor);
  dom.tabAdvisor.setAttribute('aria-selected', String(onAdvisor));
  dom.tabCompare.setAttribute('aria-selected', String(!onAdvisor));
  dom.advisorView.hidden = !onAdvisor;
  dom.compareView.hidden = onAdvisor;

  renderSidebar();
  renderSavedList();
  renderExport();
  renderDiscard();
  renderUsage();
  schedulePersist();
}

/** Enable or disable the composer without rebuilding it. */
function setComposerBusy(busy) {
  const input = $('composer-input');
  const button = $('send-btn');
  if (!input || !button) return false;

  input.disabled = busy || state.health.ok === false;
  button.disabled = input.disabled;
  // The constraint selects too (F5), or they stay live mid-request and the
  // reply comes back built to something other than what is on screen.
  document.querySelectorAll('[data-constraint]').forEach((select) => {
    select.disabled = input.disabled;
  });
  const rebuild = document.querySelector('.composer__rebuild');
  if (rebuild) rebuild.disabled = input.disabled;
  // The assumptions panel too (F10): an edit committed mid-request would stage
  // itself against the architecture being replaced.
  document
    .querySelectorAll('.assumption__btn, [data-rebuild-assumptions], .assumption__input')
    .forEach((node) => {
      node.disabled = input.disabled;
    });
  return true;
}

function setSuggestions(html) {
  const node = $('suggestions');
  if (node) node.innerHTML = html;
}

/** Add a turn to the end of the thread, leaving the ones above it alone. */
function appendTurn(html) {
  const thread = $('thread');
  if (!thread) return false;
  thread.insertAdjacentHTML('beforeend', html);
  return true;
}

/** Ask a question without rebuilding the thread it is being added to. */
function startTurn(content) {
  if (state.tab !== 'advisor' || state.alert) return false;
  if (!appendTurn(userMessage(content) + liveMessage())) return false;
  if (!setComposerBusy(true)) return false;

  setSuggestions('');
  renderChrome();
  return true;
}

/** Settle the reply that was streaming into an ordinary message. */
function finishTurn(session) {
  const live = $('live-reply');
  const index = session.messages.length - 1;
  const message = session.messages[index];
  if (!live || !message || message.role !== 'assistant') return false;

  // The context strip appears above the thread on the second question. It has
  // a host of its own so that arriving is a change to one node, not a rebuild.
  const host = $('context-host');
  if (!host) return false;
  host.innerHTML = contextStrip(session);

  // A rebuild is what closes the gap between the selectors and the architecture,
  // so the notice has to be re-read when one lands -- this is a targeted update
  // and nothing else here would clear it.
  // Both notices, not one: a correction staged before a follow-up question was
  // asked is still staged after it, and rewriting this node with only the
  // constraints notice in it would take the amber panel away and leave the
  // reader with nothing to press (F10).
  const stale = $('stale-host');
  if (stale) {
    stale.innerHTML = staleConstraintsNotice(session) + stagedAssumptionsNotice(session);
  }

  live.innerHTML = replyBody(message, `${session.id}-${index}`);
  live.removeAttribute('id');
  setComposerBusy(false);

  // The composer is not rebuilt when a turn lands, and Rebuild only exists once
  // there is an architecture to rebuild -- so on the turn that first produces
  // one, the button has to be put there. Without this it stayed missing until
  // something else forced a full render.
  const composer = document.querySelector('.composer');
  if (composer && !composer.querySelector('.composer__rebuild')) {
    composer.insertAdjacentHTML('afterbegin', rebuildButton(session));
  }

  // Every panel above this reply was editable while its architecture was the
  // newest one, and this reply supersedes them (F10). They are repainted through
  // the same function that drew them, so they come back read-only rather than
  // keeping an Edit button that would stage a correction nothing can act on.
  session.messages.forEach((older, at) => {
    if (at === index || older.role !== 'assistant' || !older.structured) return;
    const panel = document.getElementById(`assumptions-${session.id}-${at}`);
    if (panel) panel.outerHTML = assumptionsBlock(older, `${session.id}-${at}`);
  });
  setSuggestions(
    suggestionsHtml(session)
  );
  renderChrome();
  return true;
}

/** Show or hide one diagram's Mermaid source, in place. */
function toggleSource(id) {
  state.showSource[id] = !state.showSource[id];

  const button = document.querySelector(`[data-source="${CSS.escape(id)}"]`);
  const block = button && button.parentElement;
  if (!block) return false;

  const open = state.showSource[id];
  button.textContent = `${open ? 'Hide' : 'View'} diagram source`;

  const existing = block.querySelector('.source');
  if (!open) {
    if (existing) existing.remove();
    return true;
  }
  if (existing) return true;

  const source = (state.sources && state.sources[id]) || '';
  button.insertAdjacentHTML(
    'afterend',
    `<div class="source"><div class="source__head">Diagram source</div>` +
      `<pre>${esc(source)}</pre></div>`
  );
  return true;
}

/* ------------------------------------------------------------------ *
 * Actions
 * ------------------------------------------------------------------ */

/** The conversation as the API wants it: user text, and each reply's raw Markdown. */
function history(messages) {
  return messages.map((message) =>
    message.role === 'user'
      ? { role: 'user', content: message.content }
      : { role: 'assistant', content: message.raw }
  );
}

/** Open one assumption for editing. One at a time, like a rename. */
function startAssumptionEdit(key) {
  const cut = key.lastIndexOf(':');
  const id = key.slice(0, cut);
  state.editingAssumption = { id, index: Number(key.slice(cut + 1)) };
  if (!paintAssumptions(id)) render();
}

/* Keep the rewrite, or drop it where it says nothing. An edit back to the
   original is not an edit, so it is removed rather than stored as a no-op that
   would keep the amber notice up. */
function commitAssumptionEdit(key, value) {
  const cut = key.lastIndexOf(':');
  const id = key.slice(0, cut);
  const index = Number(key.slice(cut + 1));
  state.editingAssumption = null;

  const session = activeSession();
  const latest = latestArchitecture(session);
  const text = String(value || '')
    .trim()
    .slice(0, MAX_ASSUMPTION_CHARS);
  const original = latest ? unesc((latest.message.assumptions || [])[index]) : '';

  const edits = { ...(state.assumptionEdits[id] || {}) };
  if (!text || text === original) delete edits[index];
  else edits[index] = text;

  if (Object.keys(edits).length) state.assumptionEdits[id] = edits;
  else delete state.assumptionEdits[id];

  persist();
  if (!paintAssumptions(id)) render();
}

function resetAssumption(key) {
  const cut = key.lastIndexOf(':');
  const id = key.slice(0, cut);
  const index = Number(key.slice(cut + 1));
  const edits = { ...(state.assumptionEdits[id] || {}) };
  delete edits[index];
  if (Object.keys(edits).length) state.assumptionEdits[id] = edits;
  else delete state.assumptionEdits[id];
  state.editingAssumption = null;
  persist();
  if (!paintAssumptions(id)) render();
}

/* One revision for every correction made since the last one (F10).
 *
 * The staged edits are cleared before the call rather than after it: the reply
 * that comes back is a new architecture with its own assumptions, and leaving
 * the old thread's edits behind would leave the amber notice up against a
 * reply that has already answered it. */
function rebuildFromAssumptions() {
  if (state.loading) return;
  const session = activeSession();
  const latest = latestArchitecture(session);
  if (!latest) return;

  const id = `${session.id}-${latest.index}`;
  const staged = stagedAssumptions(latest.message, id);
  if (!staged.length) return;

  delete state.assumptionEdits[id];
  send(assumptionTurn(staged), true);
}

async function send(text, revise = false) {
  const content = String(text || '').trim();
  if (!content || state.loading) return;

  const session = activeSession();
  session.messages.push({ role: 'user', content });
  session.updatedAt = Date.now();

  const wasAlerting = !!state.alert;
  state.alert = null;
  state.pending = null;
  state.loading = true;
  state.streaming = { message: null, thinking: '' };
  state.savedNote = null;
  // A2: append to the thread where there is one, and rebuild where there is not
  // -- the first question of a conversation replaces the whole welcome screen.
  if (wasAlerting || !startTurn(content)) render();

  let finished = false;
  try {
    const body = { messages: history(session.messages), ...constraintsBody() };
    // Only sent when it means something. The server ignores it on a first turn.
    if (revise) body.revise = true;

    await streamRequest('/api/advise', body, (event) => {
      if (event.type === 'thinking') {
        state.streaming.thinking = event.text;
        paintLive();
      } else if (event.type === 'partial') {
        state.streaming.message = event.message;
        paintLive();
      } else if (event.type === 'done') {
        // Stamped on the reply, not on the session: a thread can hold several
        // architectures, and each was built to whatever was selected at the
        // time. That is what staleConstraintsNotice compares against.
        const built = event.message.structured
          ? { builtFor: { region: state.constraints.region, compliance: liveCompliance() } }
          : {};
        session.messages.push({ role: 'assistant', ...event.message, ...built });
        session.usage = addUsage(session.usage, event.usage);
        session.updatedAt = Date.now();
        if (!session.title) session.title = event.title || '';
        finished = true;
      }
    });
  } catch (failure) {
    state.pending = { content, revise };
    session.usage = addUsage(session.usage, failure.usage);
    if (failure.kind === 'connection') toast(failure.error);
    else state.alert = errorAlert(failure);
    if (failure.kind === 'missing_key') state.health.ok = false;
  } finally {
    state.loading = false;
    state.streaming = null;
    if (!finished || !finishTurn(session)) render();
    priceThread(session);
    focusComposer();
  }
}

/** Drop the trailing user message that a failed request left behind. */
function dropFailedTurn(session) {
  const last = session.messages[session.messages.length - 1];
  if (last && last.role === 'user') session.messages.pop();
}

/** Re-send the last user message after a failure, without duplicating it. */
async function retry() {
  const pending = state.pending;
  if (!pending) return;

  dropFailedTurn(activeSession());
  await send(pending.content, pending.revise);
}

function editPending() {
  const session = activeSession();
  const pending = state.pending;
  dropFailedTurn(session);
  if (!session.messages.length) session.title = '';

  state.alert = null;
  state.pending = null;
  render();

  const input = $('composer-input');
  if (input && pending) {
    input.value = pending.content;
    input.focus();
  }
}

/** Read the workload fields back off the form. */
function readWorkloads() {
  return state.compare.workloads.map((existing, index) => {
    const field = $(`workload-${index}`);
    return field ? field.value : existing;
  });
}

async function runCompare() {
  const workloads = readWorkloads();
  state.compare.workloads = workloads;
  state.compare.error = null;

  if (workloads.some((text) => !text.trim())) {
    state.compare.error = {
      kind: 'empty',
      error:
        'Describe every workload before comparing. An empty field leaves nothing to ' +
        'weigh up.',
    };
    render();
    return;
  }

  state.compare.loading = true;
  state.compare.usage = null; // a fresh run replaces every result, and the cost
  // Every column starts empty and fills in as each architecture is written.
  state.compare.results = workloads.map(() => null);
  state.compare.thinking = workloads.map(() => '');
  state.compare.title = '';
  state.savedNote = null;
  render();

  const body = { workloads, ...constraintsBody() };

  try {
    await streamRequest('/api/compare', body, (event) => {
      // Whatever column the server says, bounded by how many there are. This
      // used to clamp to 0 or 1, which silently painted a third column's
      // reply over the first.
      const index = Number(event.index) || 0;
      if (index < 0 || index >= workloads.length) return;
      if (event.type === 'thinking') {
        state.compare.thinking[index] = event.text;
        paintCompare();
      } else if (event.type === 'partial') {
        state.compare.results[index] = event.message;
        paintCompare();
      } else if (event.type === 'done') {
        state.compare.results = event.results;
        state.compare.usage = addUsage(null, event.usage);
        state.compare.title = event.title || '';
      }
    });
  } catch (failure) {
    state.compare.error = failure;
    state.compare.results = null; // half a comparison is not a comparison
    state.compare.usage = addUsage(state.compare.usage, failure.usage);
    if (failure.kind === 'connection') toast(failure.error);
    if (failure.kind === 'missing_key') state.health.ok = false;
  } finally {
    state.compare.loading = false;
    render();
    (state.compare.results || []).forEach((result, index) => {
      if (result) requestEstimate(`compare-${index}`, result.raw);
    });
  }
}

/* ---- Saved conversations ---- */

/** Show a failure the same way everywhere: a toast if the server is down. */
function reportFailure(failure) {
  if (failure.kind === 'connection') toast(failure.error);
  else state.alert = errorAlert(failure);
}

async function refreshSaved() {
  // /api/search with nothing set returns the same list /api/conversations does,
  // but it also returns the tags in use, which the filter chips need. So the
  // search endpoint is used either way and the plain listing is the fallback.
  const query = new URLSearchParams();
  if (state.query.text.trim()) query.set('q', state.query.text.trim());
  if (state.query.tier) query.set('tier', state.query.tier);
  if (state.query.service) query.set('service', state.query.service);
  if (state.query.tag) query.set('tag', state.query.tag);

  state.searching = activeFilters();
  try {
    const payload = await request(`/api/search?${query.toString()}`);
    state.saved = payload.conversations;
    state.tags = payload.tags || [];
  } catch (failure) {
    state.saved = [];
    state.tags = [];
  }
  render();
}

/** Open a saved conversation, rebuilt into whichever view it was captured from. */
async function openSaved(id) {
  let payload;
  try {
    payload = await request(`/api/conversations/${id}`);
  } catch (failure) {
    reportFailure(failure);
    render();
    return;
  }

  state.openedId = id;
  state.alert = null;
  state.pending = null;
  state.savedNote = null;

  // Files saved before cost accounting existed have no total, and show none.
  const savedUsage = payload.usage ? addUsage(null, payload.usage) : null;

  // Put the selectors back where they were when this was built (F5), so a
  // follow-up asks under the same constraints the architecture was made for.
  state.constraints = {
    region: payload.region || '',
    compliance: payload.compliance || '',
  };

  if (payload.mode === 'compare') {
    // A saved comparison is a brief and a recommendation an option, in order.
    const users = payload.messages.filter((m) => m.role === 'user');
    const replies = payload.messages.filter((m) => m.role === 'assistant');
    const width = Math.min(replies.length, MAX_COMPARE);
    state.compare.workloads = Array.from(
      { length: Math.max(width, MIN_COMPARE) },
      (_unused, index) => (users[index] || {}).content || ''
    );
    state.compare.results = width >= MIN_COMPARE ? replies.slice(0, width) : null;
    state.compare.thinking = state.compare.workloads.map(() => '');
    state.compare.error = null;
    state.compare.usage = savedUsage;
    state.compare.title = payload.title;
    state.tab = 'compare';
    // `|| []`, because results is deliberately null when there are fewer replies
    // than a comparison needs -- a hand-edited export, or a JSON file imported by
    // migrate.py. Iterating it threw, and the view did not render at all.
    (state.compare.results || []).forEach((result, index) => {
      if (result) requestEstimate(`compare-${index}`, result.raw);
    });
  } else {
    // Reopen as a live session, so follow-up questions carry on from here.
    const saved = Date.parse(payload.savedAt) || Date.now();
    const session = {
      id: `saved-${id}`,
      title: payload.title,
      createdAt: saved,
      updatedAt: saved,
      messages: payload.messages,
      usage: savedUsage,
      savedId: id,
    };
    state.sessions = state.sessions.filter(
      (existing) => existing.id !== session.id && existing.messages.length
    );
    state.sessions.unshift(session);
    state.activeId = session.id;
    state.tab = 'advisor';
    priceThread(session);
  }

  render();
}

function startRename(id) {
  state.renaming = id;
  state.deleting = null;
  render();
}

/** Two-step delete: the bin arms it, a second click confirms. */
function startDelete(id) {
  state.deleting = id;
  state.renaming = null;
  render();
}

async function confirmDelete(id) {
  state.deleting = null;
  try {
    await request(`/api/conversations/${id}`, null, 'DELETE');
    state.saved = state.saved.filter((item) => item.id !== id);
    if (state.openedId === id) state.openedId = null;
    // The conversation stays on screen, it just is not saved any more.
    const session = state.sessions.find((entry) => entry.savedId === id);
    if (session) delete session.savedId;
    state.savedNote = null;
  } catch (failure) {
    reportFailure(failure);
  }
  render();
}

async function commitRename(id, title) {
  const clean = String(title || '').trim();
  state.renaming = null;

  const item = state.saved.find((entry) => entry.id === id);
  if (!clean || !item || clean === item.title) {
    render();
    return;
  }

  try {
    const payload = await request(`/api/conversations/${id}/title`, { title: clean });
    item.title = payload.title;
    item.titled = true;
    // Keep an open session's heading in step with the rename.
    const session = state.sessions.find((entry) => entry.savedId === id);
    if (session) session.title = payload.title;
  } catch (failure) {
    reportFailure(failure);
  }
  render();
}

async function saveConversation() {
  const session = activeSession();
  const compareMode = state.tab === 'compare' && state.compare.results;

  const messages = compareMode
    ? state.compare.results.flatMap((result, index) => [
        { role: 'user', content: state.compare.workloads[index] || '' },
        { role: 'assistant', content: result.raw },
      ])
    : history(session.messages);

  if (!messages.length) return;

  // The server names an unnamed conversation; this only sends a title when
  // there is one worth keeping, which means one you gave it.
  const title = compareMode ? state.compare.title : session.title;

  try {
    const payload = await request('/api/save', {
      messages,
      mode: compareMode ? 'compare' : 'advise',
      title: title || undefined,
      usage: (compareMode ? state.compare.usage : session.usage) || undefined,
      ...constraintsBody(),
    });
    state.savedNote = `Saved as “${payload.title}”`;
    state.openedId = payload.id;
    if (!compareMode) {
      session.savedId = payload.id;
      // What was saved, so two more questions make it unsaved again.
      session.savedCount = session.messages.length;
    }
    await refreshSaved(); // renders
  } catch (failure) {
    reportFailure(failure);
    render();
  }
}

/* Everything that would throw the live conversation away goes through here.
   `pending` is the thing to do once the question is answered, so the dialog does
   not have to know what it is guarding. */
function confirmDiscard(what, run) {
  const session = unsavedWork();
  if (!session) return run();
  state.discard = { what, run };
  render();
}

function closeDiscard() {
  state.discard = null;
  render();
}

function discardAnyway() {
  const pending = state.discard;
  state.discard = null;
  if (pending) pending.run();
}

async function saveThenDiscard() {
  const pending = state.discard;
  state.discard = null;
  await saveConversation();
  // Only go ahead if the save actually worked: losing the conversation because
  // the store was unreachable is the thing this dialog exists to prevent.
  if (pending && !state.alert) pending.run();
  else render();
}

function clearSession() {
  confirmDiscard('clear this session', doClearSession);
}

function doClearSession() {
  // Dropped rather than left in the list. The warning says the conversation will
  // be lost, so a row for it still sitting in the sidebar afterwards would make
  // that warning a lie. A saved one is still in Saved, which is where it lives.
  const going = activeSession();
  if (going.messages.length) {
    state.sessions = state.sessions.filter((session) => session !== going);
    newSession();
  }

  state.alert = null;
  state.pending = null;
  state.savedNote = null;
  state.openedId = null;
  state.compare.results = null;
  state.compare.error = null;
  state.compare.usage = null;
  state.compare.thinking = ['', ''];
  state.compare.title = '';
  render();
  focusComposer();
}

async function loadSetupGuide() {
  try {
    const payload = await request('/api/readme');
    state.setupHtml = payload.html;
  } catch (failure) {
    state.setupHtml = '<p>Could not read the project README.</p>';
  }
  render();
}

async function checkHealth() {
  try {
    const payload = await request('/api/health');
    // `exports` says what this machine can write (F3). Defaulted rather than
    // trusted, so an older server or a failed field never breaks the dialog.
    state.health = { ...payload, exports: { pdf: true, ...(payload.exports || {}) } };
    if (!payload.ok) {
      state.alert = keyAlert();
    } else if (state.alert && state.alert.kind === 'missing_key') {
      state.alert = null;
      state.setupHtml = null;
    }
  } catch (failure) {
    toast(failure.error);
  }
  render();
}

function focusComposer() {
  if (state.tab !== 'advisor') return;
  const input = $('composer-input');
  if (input && !input.disabled) input.focus();
}

/** Send whatever is in the composer, clearing it first so it cannot double-send. */
function sendComposer(input) {
  if (!input) return;
  const value = input.value;
  input.value = '';
  send(value);
}

/* ------------------------------------------------------------------ *
 * Wiring
 * ------------------------------------------------------------------ */

function switchTab(tab) {
  state.tab = tab;
  render();
  focusComposer();
}

dom.tabAdvisor.addEventListener('click', () => switchTab('advisor'));
dom.tabCompare.addEventListener('click', () => switchTab('compare'));
dom.saveBtn.addEventListener('click', saveConversation);
$('clear-btn').addEventListener('click', clearSession);

const ACTIONS = {
  retry,
  edit: editPending,
  recheck: checkHealth,
  guide: loadSetupGuide,
  recompare: runCompare,
};

/* One delegated handler, because the views are re-rendered wholesale.
 *
 * Every click the app answers, as one map from dataset key to what it does. This
 * was two hand-maintained lists -- a selector string and a chain of
 * `if (data.x) return ...` -- that had to be kept in step by hand, and adding an
 * attribute to one and not the other gives a button that silently does nothing.
 * The selector is derived from these keys now, so there is one list to maintain
 * and the two cannot disagree.
 *
 * A handler is called with (value, target, event). Order is insertion order and
 * the first matching key wins, the way the chain did; no element carries two of
 * these keys, so it does not currently matter which.
 */
const CLICK_HANDLERS = {
  example: (value) => send(EXAMPLES[Number(value)]),
  // The chip carries its own text (F13), rather than an index into a list that
  // no longer exists once the model writes them.
  suggestion: (value) => send(value),
  rebuild: () => send(REVISE_TURN, true),
  rebuildAssumptions: () => rebuildFromAssumptions(),
  assumption: (value) => startAssumptionEdit(value),
  assumptionReset: (value) => resetAssumption(value),
  dropWorkload: (value) => dropWorkload(Number(value)),
  tag: (value) => startTagging(Number(value)),
  untag: (value, target) => untag(Number(value), target.dataset.tagName || ''),
  filterTag: (value) => setFilter('tag', value),
  filterTier: (value) => setFilter('tier', value),
  clearFilters: () => clearFilters(),
  open: (value) => openSaved(Number(value)),
  rename: (value) => startRename(Number(value)),
  delete: (value) => startDelete(Number(value)),
  deleteYes: (value) => confirmDelete(Number(value)),
  deleteNo: () => {
    state.deleting = null;
    render();
  },
  session: (value) => {
    state.activeId = value;
    state.openedId = null;
    state.alert = null;
    state.pending = null;
    state.savedNote = null;
    render();
  },
  source: (value) => {
    if (!toggleSource(value)) render();
  },
  downloadSvg: (value) => downloadDiagramSvg(value),
  downloadPng: (value) => downloadDiagramPng(value),
  reprice: (value) => repriceEstimate(value),
  cost: (value) => {
    // Once the reader has chosen, their choice sticks: state.costOpen is only
    // written here, and estimateBody falls back to the coverage default while
    // the key is absent. paintEstimate rather than render, for the same reason
    // the diagram toggle repaints in place (A2).
    const estimate = state.estimates[value];
    const wasOpen =
      value in state.costOpen ? state.costOpen[value] : !mostlyEstimated(estimate || {});
    state.costOpen[value] = !wasOpen;
    paintEstimate(value);
  },
  jump: (_value, target, event) => {
    event.preventDefault();
    const node = document.querySelector(target.getAttribute('href'));
    if (node) node.scrollIntoView({ behavior: 'smooth', block: 'start' });
  },
  exportCancel: () => closeExport(),
  exportGo: () => runExport(),
  exportRevise: () => reviseFromExport(),
  usageClose: () => closeUsage(),
  usageMonth: (value) => showMonth(value),
  discardCancel: () => closeDiscard(),
  discardAnyway: () => discardAnyway(),
  discardSave: () => saveThenDiscard(),
  action: (value) => {
    const run = ACTIONS[value];
    if (run) run();
  },
};

/* ------------------------------------------------------------------ *
 * What this has cost (F14)
 *
 * The ledger has recorded every call since the first release and the only thing
 * that ever read it was the daily ceiling, which looks at today and says no. A
 * dialog rather than a sidebar section: a month of days needs the width, and the
 * sidebar's one scrolling section is already the saved list.
 * ------------------------------------------------------------------ */

/** The month before or after one written YYYY-MM. */
function stepMonth(month, by) {
  const [year, number] = month.split('-').map(Number);
  // Date does the arithmetic, including December to January, on a month index
  // that is zero-based -- so `number` is already the next month along.
  const moved = new Date(Date.UTC(year, number - 1 + by, 1));
  return `${moved.getUTCFullYear()}-${String(moved.getUTCMonth() + 1).padStart(2, '0')}`;
}

function openUsage() {
  state.usage = { month: '', report: null, loading: true };
  render();
  refreshUsage();
}

function closeUsage() {
  state.usage = null;
  render();
}

function showMonth(month) {
  if (!state.usage) return;
  state.usage = { month, report: null, loading: true };
  render();
  refreshUsage();
}

/** Read the month back off the ledger.
 *
 * A failure empties the panel rather than raising an alert, the way
 * refreshSaved() does: this is a read-only view of something the server owns,
 * and nothing the reader typed is at stake.
 */
async function refreshUsage() {
  const asked = state.usage;
  if (!asked) return;
  const query = asked.month ? `?month=${encodeURIComponent(asked.month)}` : '';
  try {
    const report = await request(`/api/usage${query}`);
    // The reader may have stepped to another month, or closed the panel, while
    // this was in flight. Only the answer to the question still on screen counts.
    if (state.usage !== asked) return;
    state.usage = { month: report.month, report, loading: false };
  } catch (failure) {
    if (state.usage !== asked) return;
    state.usage = { month: asked.month, report: null, loading: false };
  }
  render();
}

/** One row of the spend tables: a name, then calls, tokens and cost. */
function spendRow(name, total, className) {
  return (
    `<tr${className ? ` class="${className}"` : ''}>` +
    `<th scope="row">${esc(name)}</th>` +
    `<td>${esc(tokens(total.calls))}</td>` +
    `<td>${esc(tokens(total.tokens))}</td>` +
    `<td>${esc(costLabel(total))}</td></tr>`
  );
}

const SPEND_HEAD =
  '<thead><tr><th scope="col">&nbsp;</th><th scope="col">Calls</th>' +
  '<th scope="col">Tokens</th><th scope="col">Cost</th></tr></thead>';

/** A month's days, with the kinds folded together. `days` is per day and kind. */
function spendByDay(days) {
  const folded = new Map();
  days.forEach((entry) => {
    const running = folded.get(entry.day) || { calls: 0, tokens: 0, costUsd: 0, priced: true };
    running.calls += entry.calls;
    running.tokens += entry.tokens;
    running.costUsd += entry.costUsd;
    running.priced = running.priced && entry.priced !== false;
    folded.set(entry.day, running);
  });
  return [...folded.entries()].sort(([a], [b]) => (a < b ? -1 : 1));
}

/** Today against the ceiling: the one place it is visible before it refuses. */
function ceilingLine(report) {
  const today = report.today || { tokens: 0, calls: 0, costUsd: 0 };
  const spent =
    `Today: ${tokens(today.tokens)} tokens across ${plural(today.calls, 'call')}, ` +
    `${costLabel(today)}.`;
  if (!report.ceiling) return `${spent} No ceiling is set.`;
  const used = Math.min(100, Math.round((today.tokens / report.ceiling) * 100));
  return (
    `${spent} That is ${used}% of today's ceiling of ${tokens(report.ceiling)} tokens, ` +
    'which resets at midnight, UK time.'
  );
}

function renderUsage() {
  const host = $('usage-dialog');
  if (!host) return;
  const panel = state.usage;
  if (!panel) {
    host.innerHTML = '';
    return;
  }

  const report = panel.report;
  const month = report ? report.month : panel.month;
  let body;
  if (panel.loading) {
    body = `<p class="dialog__note">Reading the ledger…</p>`;
  } else if (!report) {
    body = `<p class="dialog__note dialog__note--error">The ledger could not be read.</p>`;
  } else if (!report.days.length) {
    body = `<p class="dialog__note">Nothing was spent in ${esc(month)}.</p>`;
  } else {
    body =
      `<span class="eyebrow">By kind</span>` +
      `<table class="spend">${SPEND_HEAD}<tbody>` +
      report.kinds.map((entry) => spendRow(entry.kind, entry)).join('') +
      spendRow('Total', report.totals, 'spend__total') +
      `</tbody></table>` +
      `<span class="eyebrow">By day</span>` +
      `<table class="spend">${SPEND_HEAD}<tbody>` +
      spendByDay(report.days)
        .map(([day, total]) => spendRow(day, total))
        .join('') +
      `</tbody></table>`;
  }

  const ceiling = report ? `<p class="dialog__note">${esc(ceilingLine(report))}</p>` : '';

  host.innerHTML =
    `<div class="overlay" data-usage-backdrop="1">` +
    `<div class="dialog" role="dialog" aria-modal="true" aria-labelledby="usage-title">` +
    `<div class="dialog__head">` +
    `<span class="eyebrow eyebrow--primary">Ledger</span>` +
    `<h2 class="dialog__title" id="usage-title">What this has cost</h2>` +
    `<p class="dialog__lead">Every call the CLI and this page have made, by kind and by ` +
    `day. List price in US dollars, before any discount.</p></div>` +
    `<div class="dialog__body">` +
    `<div class="spend__months">` +
    `<button class="btn btn--ghost btn--alert" data-usage-month="${esc(stepMonth(month, -1))}">` +
    `&larr; ${esc(stepMonth(month, -1))}</button>` +
    `<span class="spend__month">${esc(month)}</span>` +
    `<button class="btn btn--ghost btn--alert" data-usage-month="${esc(stepMonth(month, 1))}">` +
    `${esc(stepMonth(month, 1))} &rarr;</button></div>` +
    `${body}${ceiling}</div>` +
    `<div class="dialog__foot">` +
    `<button class="btn btn--primary btn--alert" data-usage-close="1">Close</button>` +
    `</div></div></div>`;
}

/* The buttons that are addressed by id rather than by a data attribute, because
   index.html holds them and they are not rendered from state. */
const CLICK_IDS = {
  'send-btn': () => sendComposer($('composer-input')),
  'add-workload': () => addWorkload(),
  'compare-btn': () => runCompare(),
  'export-btn': () => openExport(),
  'usage-btn': () => openUsage(),
};

const CLICK_KEYS = Object.keys(CLICK_HANDLERS);

/** `rebuildAssumptions` -> `data-rebuild-assumptions`, the way the DOM maps it. */
function dataAttribute(key) {
  return `data-${key.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`)}`;
}

const CLICK_TARGETS = CLICK_KEYS.map((key) => `[${dataAttribute(key)}]`)
  .concat(Object.keys(CLICK_IDS).map((id) => `#${id}`))
  .join(', ');

document.addEventListener('click', (event) => {
  // The backdrops are deliberately not in CLICK_TARGETS: they are the one place
  // where hitting exactly this element, and not something inside it, is the
  // gesture. So they are answered from event.target, before closest() widens the
  // search to anything the click happened to land inside.
  if (event.target.dataset && event.target.dataset.discardBackdrop) return closeDiscard();
  if (event.target.dataset && event.target.dataset.exportBackdrop) return closeExport();
  if (event.target.dataset && event.target.dataset.usageBackdrop) return closeUsage();

  const target = event.target.closest(CLICK_TARGETS);
  if (!target) return;

  const byId = CLICK_IDS[target.id];
  if (byId) return byId();

  const data = target.dataset;
  // `key in data`, not a truthiness test: several of these carry a value that is
  // legitimately empty or zero. data-example="0" is the first example, and
  // data-filter-tier="" is a filter being cleared.
  for (const key of CLICK_KEYS) {
    if (key in data) return CLICK_HANDLERS[key](data[key], target, event);
  }
});

document.addEventListener('keydown', (event) => {
  if (state.discard && event.key === 'Escape') {
    event.preventDefault();
    return closeDiscard();
  }
  if (state.export && event.key === 'Escape') {
    event.preventDefault();
    return closeExport();
  }
  if (state.usage && event.key === 'Escape') {
    event.preventDefault();
    return closeUsage();
  }

  const renaming = event.target.dataset && event.target.dataset.renameInput;
  if (renaming) {
    if (event.key === 'Enter') {
      event.preventDefault();
      commitRename(Number(renaming), event.target.value);
    } else if (event.key === 'Escape') {
      event.preventDefault();
      state.renaming = null;
      render();
    }
    return;
  }

  const assumption = event.target.dataset && event.target.dataset.assumptionInput;
  if (assumption) {
    if (event.key === 'Enter') {
      event.preventDefault();
      commitAssumptionEdit(assumption, event.target.value);
    } else if (event.key === 'Escape') {
      event.preventDefault();
      const open = state.editingAssumption;
      state.editingAssumption = null;
      if (!open || !paintAssumptions(open.id)) render();
    }
    return;
  }

  const tagging = event.target.dataset && event.target.dataset.tagInput;
  if (tagging) {
    if (event.key === 'Enter') {
      event.preventDefault();
      commitTag(Number(tagging), event.target.value);
    } else if (event.key === 'Escape') {
      event.preventDefault();
      state.tagging = null;
      render();
    }
    return;
  }

  // Escape in the search box clears it, the way it does in a browser's own find
  // bar. Anything else typed there is handled by the input listener.
  if (event.target.id === 'saved-search' && event.key === 'Escape') {
    event.preventDefault();
    return clearFilters();
  }

  if (event.key === 'Enter' && event.target.id === 'composer-input') {
    event.preventDefault();
    sendComposer(event.target);
  }
});

// Clicking away from a rename box commits it, the same as pressing Enter.
document.addEventListener(
  'blur',
  (event) => {
    const renaming = event.target.dataset && event.target.dataset.renameInput;
    if (renaming && state.renaming === Number(renaming)) {
      commitRename(Number(renaming), event.target.value);
    }
    const assumption = event.target.dataset && event.target.dataset.assumptionInput;
    if (assumption && state.editingAssumption) {
      commitAssumptionEdit(assumption, event.target.value);
    }
  },
  true
);

/* Everything that answers a `change` rather than a click: the constraint selects
   (F5) and the export dialog's checkboxes (F3). A `change` listener, not the
   delegated click handler, because a select moved by the keyboard never fires
   click and the CSP forbids an inline handler.

   One listener rather than the two this used to have. They were registered
   separately with an `input` handler in between, which read as three unrelated
   things rather than one place where a changed control is dealt with. */
document.addEventListener('change', (event) => {
  const data = event.target.dataset || {};

  if (data.constraint === 'region' || data.constraint === 'compliance') {
    state.constraints[data.constraint] = event.target.value;
    // Re-rendered so the other tab's copy of the row agrees with this one.
    render();
    return;
  }

  if (state.export && (data.exportFormat || data.exportToggle)) {
    captureExport();
    if (data.exportFormat) state.export.formats[data.exportFormat] = event.target.checked;
    else state.export[data.exportToggle] = event.target.checked;
    render();
  }
});

// Keep the compare fields in state so a tab switch does not lose them.
document.addEventListener('input', (event) => {
  const workload = /^workload-(\d+)$/.exec(event.target.id || '');
  if (workload) state.compare.workloads[Number(workload[1])] = event.target.value;
  else if (event.target.id === 'saved-search') {
    state.query.text = event.target.value;
    scheduleSearch();
  }
  // The filename in the dialog's footer is built from the client name, so it
  // follows what is being typed rather than waiting for the export.
  else if (event.target.id === 'export-client' || event.target.id === 'export-prepared-by') {
    captureExport();
    const name = document.querySelector('.dialog__name');
    if (name && state.export) name.textContent = exportFilename(state.export);
  }
});

// The debounce above is there so that render() can call it freely; a reload can
// happen inside that window, and leaving the page is exactly when the autosave
// matters most, so the pending write is flushed on the way out. `pagehide` and
// `visibilitychange` rather than `beforeunload`: they fire in the cases that
// matter, including a tab being closed on a phone, and they do not put the
// page into the unload-confirmation machinery.
window.addEventListener('pagehide', persist);
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') persist();
});

// A3: pick up where the last tab left off, including the question it was in
// the middle of asking.
const draft = restore();
/* A reload throws the live conversation away unless it was saved. The browser
   will only show its own generic wording here -- a page cannot choose the text --
   but the prompt itself is the point, and A3's restore covers the rest. */
window.addEventListener('beforeunload', (event) => {
  if (!unsavedWork()) return;
  event.preventDefault();
  // Chrome needs returnValue set; the string is ignored by every modern browser.
  event.returnValue = '';
});

if (!state.sessions.length) newSession();
render();
priceThread(activeSession());

const composerInput = $('composer-input');
if (composerInput && draft) composerInput.value = draft;

checkHealth();
refreshSaved();
