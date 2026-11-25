// Briefing screen: list and detail views over the read-only JSON API (same origin).
import {
  chip, collapsible, el, formatDate, formatPublished, hidden, label, list, notStated, NOT_STATED,
  outLink,
} from "./dom.js";

const API = "/api/v1";
const PAGE = 20;
const main = () => document.getElementById("main");
const TITLE = "News briefings";

// Navigation token: each route change bumps it; responses for an older token are dropped.
let nav = 0;
const current = (token) => token === nav;

class HttpError extends Error {
  constructor(status) { super(`HTTP ${status}`); this.status = status; }
}

async function getJson(path) {
  // Always revalidate: a re-checked analysis must never be shown from a stale cache.
  const response = await fetch(API + path, {
    headers: { Accept: "application/json" },
    cache: "no-cache",
  });
  if (!response.ok) throw new HttpError(response.status);
  return response.json();
}

// Focus moves to main only on a route change (focus = true), never on a repaint.
function show(focus, ...nodes) {
  const root = main();
  // Skip empty slots (e.g. no review banner): replaceChildren would print "null".
  root.replaceChildren(...nodes.filter((n) => n !== null && n !== undefined));
  if (focus) root.focus({ preventScroll: true });
}

function notFoundView(message) {
  document.title = `Not found - ${TITLE}`;
  return el("div", { class: "banner error", role: "alert" }, `${message} `,
    el("a", { href: "#/" }, "Back to briefings"));
}

function errorView(error, retry, notFound) {
  if (error instanceof HttpError && error.status === 404) return notFoundView(notFound);
  document.title = `Error - ${TITLE}`;
  return el("div", { class: "banner error", role: "alert" },
    "Could not reach the API. ",
    el("button", { type: "button", onclick: retry }, "Retry"));
}

// --- list ---------------------------------------------------------------------------

const state = { items: [], cursor: null, loaded: false, query: "", status: "" };

function statusChips(reportType, eventStatus, review) {
  return [
    reportType ? chip(label(reportType)) : null,
    eventStatus ? chip(label(eventStatus)) : null,
    review ? chip("Needs review", "review") : null,
  ];
}

function card(item) {
  const places = (item.places || []).slice(0, 3).map((p) => p.name_as_written);
  const actors = (item.actors || []).slice(0, 3).map((a) => a.full_name);
  const source = item.source?.name || "original article";
  return el("li", { class: "card" },
    el("div", { class: "meta" },
      el("time", { datetime: item.published_at }, formatPublished(item.published_at)),
      statusChips(item.report_type, item.event_status, item.review_required)),
    el("h2", {}, el("a", { href: `#/article/${item.article_id}` }, item.headline || item.title)),
    el("div", { class: "facts" },
      el("span", {}, "When: ", formatDate(item.event_date) || "date not stated"),
      el("span", {}, "Where: ", places.length ? places.join(", ") : "not stated"),
      el("span", {}, "Who: ", actors.length ? actors.join(", ") : "not stated")),
    el("p", { class: "source" }, "Read at ", outLink(item.canonical_url, source)));
}

function matches(item) {
  if (state.status && item.event_status !== state.status) return false;
  if (!state.query) return true;
  const hay = [item.headline, item.title, ...(item.places || []).map((p) => p.name_as_written),
    ...(item.actors || []).map((a) => a.full_name)].join(" ").toLowerCase();
  return hay.includes(state.query);
}

// The toolbar is built once per visit; later updates repaint only cards, options and count.
let ui = null;

function buildList() {
  const search = el("input", {
    type: "search", "aria-label": "Search loaded briefings", placeholder: "Search loaded briefings",
    value: state.query,
    oninput: () => { state.query = search.value.trim().toLowerCase(); paintCards(); },
  });
  const select = el("select", { "aria-label": "Filter by event status",
    onchange: () => { state.status = select.value; paintCards(); } });
  const cards = el("ul", { class: "cards" });
  const count = el("p", { class: "muted", role: "status" });
  const more = el("button", { type: "button", onclick: () => loadMore(nav, true) }, "Load more");
  ui = { search, select, cards, count, more };
  show(true, el("h1", {}, "Latest briefings"),
    el("div", { class: "toolbar" }, search, select), count, cards, more);
}

function paintOptions() {
  const statuses = [...new Set(state.items.map((i) => i.event_status).filter(Boolean))].sort();
  if (state.status && !statuses.includes(state.status)) statuses.push(state.status);
  ui.select.replaceChildren(el("option", { value: "" }, "All statuses"),
    ...statuses.map((s) => el("option", { value: s, selected: s === state.status }, label(s))));
}

function paintCards() {
  const visible = state.items.filter(matches);
  ui.cards.replaceChildren(...(state.items.length === 0
    ? [el("li", { class: "muted" }, "No briefings yet.")] : visible.map(card)));
  ui.count.textContent = `${visible.length} of ${state.items.length} loaded briefing(s) shown.`;
  ui.more.hidden = !state.cursor;
  ui.more.disabled = false;
  ui.more.textContent = "Load more";
}

async function loadMore(token, focusNew) {
  if (ui && focusNew) { ui.more.disabled = true; ui.more.textContent = "Loading..."; }
  try {
    const query = `?limit=${PAGE}` + (state.cursor ? `&cursor=${encodeURIComponent(state.cursor)}` : "");
    const page = await getJson(`/briefings${query}`);
    if (!current(token)) return;
    const before = state.items.length;
    state.items.push(...page.items);
    state.cursor = page.next_cursor;
    state.loaded = true;
    if (!ui) buildList();
    paintOptions();
    paintCards();
    if (focusNew) {
      // Focus the first newly loaded card's link, if it is visible under the filter.
      const fresh = state.items.slice(before).find(matches);
      const link = fresh && ui.cards.querySelector(`a[href="#/article/${fresh.article_id}"]`);
      if (link) link.focus();
    }
  } catch (error) {
    if (!current(token)) return;
    ui = null;
    show(true, errorView(error, () => loadMore(nav, false), "The briefings list was not found."));
  }
}

function listView(token) {
  document.title = TITLE;
  ui = null;
  if (state.loaded) { buildList(); paintOptions(); paintCards(); }
  else {
    show(true, el("p", { class: "muted" }, "Loading briefings..."));
    loadMore(token, false);
  }
}

// --- detail -------------------------------------------------------------------------

const INVOLVEMENT = ["central", "participant", "quoted", "mentioned"];
const PLACE_ROLES = ["event_location", "actor_location", "related_event", "mentioned"];

function groupBy(items, key, order) {
  const groups = new Map(order.map((k) => [k, []]));
  for (const item of items) {
    const k = item[key] || "other";
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(item);
  }
  return [...groups].filter(([, v]) => v.length);
}

// Panel keys whose content was not checked against the article. `$.overview.*` paths
// (the headline) map to "headline", which is shown as the page heading.
let unverified = new Set();
const UNVERIFIED = "Not checked against the article.";

function panel(title, ...content) {
  return el("section", { class: "panel" }, el("h2", {}, title), note(title), ...content);
}

function unverifiedKey(path) {
  const key = String(path).replace(/^\$\./, "").split(/[.[]/)[0].toLowerCase();
  return key === "overview" ? "headline" : key;
}

function note(title) {
  return unverified.has(title.toLowerCase().replace(/ /g, "_"))
    ? el("p", { class: "unverified" }, UNVERIFIED) : null;
}

// The publication timestamp as a UTC calendar date (YYYY-MM-DD).
function utcDate(iso) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? String(iso).slice(0, 10) : date.toISOString().slice(0, 10);
}

// A date counts as "happened" when it is on or before publication, at its own precision.
// Event dates are calendar dates; publication is converted to its UTC date to match.
function happened(date, published) {
  const pub = utcDate(published);
  const d = String(date).slice(0, 10);
  return d <= pub.slice(0, d.length);
}

function timelineList(entries, cls) {
  return el("ol", { class: `timeline ${cls}` }, entries.map((e) => el("li", {},
    el("span", { class: "when" }, e.date ? formatDate(e.date) : "Undated"), e.event)));
}

function whenPanel(b, published) {
  const timeline = b.when?.timeline || [];
  const past = timeline.filter((e) => !e.date || happened(e.date, published));
  const future = timeline.filter((e) => e.date && !happened(e.date, published));
  const predictions = (b.claims || []).filter((c) => c.kind === "prediction")
    .map((c) => ({ date: null, event: c.text }));
  const expected = [...future, ...predictions];
  return panel("When",
    el("p", {}, el("strong", {}, "Event date: "), formatDate(b.when?.event_date?.value) || NOT_STATED),
    el("h3", {}, "Happened"), past.length ? timelineList(past, "happened") : notStated(),
    el("h3", {}, "Expected"), expected.length ? timelineList(expected, "expected") : notStated());
}

function wherePanel(places) {
  if (!places.length) return panel("Where", notStated());
  return panel("Where", groupBy(places, "role", PLACE_ROLES).map(([role, items]) => [
    el("h3", {}, label(role)), list(items, (p) => p.name_as_written)]));
}

function whoPanel(actors) {
  if (!actors.length) return panel("Who", notStated());
  return panel("Who", groupBy(actors, "involvement", INVOLVEMENT).map(([inv, items]) => [
    el("h3", {}, label(inv)),
    list(items, (a) => el("span", { class: "actor" }, actorTag(a.type),
      a.full_name || a.name_as_written,
      a.role ? el("span", { class: "attr" }, ` (${a.role}; ${label(a.type)})`) : null))]));
}

const ACTOR_TYPES = {
  person: "P", government_body: "G", political_party: "Pa", company: "Co", ngo: "N",
  international_org: "I", court: "Ct", media: "M", group: "Gr", country: "C", other: "?",
};

// Visible short tag is decoration; the full type is read out from a visually-hidden label.
function actorTag(type) {
  const key = String(type || "other").toLowerCase();
  return el("span", { class: "tag" },
    el("span", { "aria-hidden": "true" }, `[${ACTOR_TYPES[key] || "?"}]`),
    hidden(`${label(key)}: `));
}

function claimsPanel(claims) {
  if (!claims.length) return panel("Claims", notStated());
  return panel("Claims", el("ul", { class: "cards" }, claims.map((c) => el("li",
    { class: `claim k-${c.kind}` },
    chip(c.kind === "allegation" ? "allegation, not a finding" : label(c.kind), c.kind), " ",
    c.text,
    c.attributed_to ? el("div", { class: "attr" }, `according to ${c.attributed_to}`) : null))));
}

function detailView(id, token) {
  document.title = `Loading briefing - ${TITLE}`;
  show(true, el("p", { class: "muted" }, "Loading briefing..."));
  getJson(`/articles/${encodeURIComponent(id)}/briefing`).then((d) => {
    if (!current(token)) return;
    const b = d.briefing;
    unverified = new Set(Array.isArray(d.unverified_fields) ? d.unverified_fields.map(unverifiedKey) : []);
    const o = b.overview || {};
    document.title = `${o.headline || d.title} - Briefing`;
    const source = d.source?.name || "original article";
    const header = el("header", {},
      el("a", { href: "#/" }, "Back to briefings"),
      el("h1", {}, o.headline || d.title),
      o.headline ? note("Headline") : null,
      el("div", { class: "meta" },
        el("span", {}, d.source?.name || ""),
        el("time", { datetime: d.published_at }, `Published ${formatPublished(d.published_at)}`),
        statusChips(o.report_type, o.event_status, d.review_required)),
      outLink(d.source_url, `Open original article (${source})`, "origin"));
    const banner = d.removed_items > 0 ? el("div", { class: "banner", role: "note" },
      `${d.removed_items} item(s) removed (unverified quotes or copied wording)`) : null;
    const why = list(b.why, (c) => [c.text, c.attributed_to
      ? el("span", { class: "attr" }, ` (according to ${c.attributed_to})`) : null]);
    const impact = list(b.impact, (i) => [el("strong", {}, `${label(i.area)}: `), i.text, " ",
      chip(label(i.certainty))]);
    show(false, header, banner, el("div", { class: "grid" },
      panel("What", b.what?.main_event ? el("p", {}, b.what.main_event) : notStated(),
        el("h3", {}, "Secondary events"), list(b.what?.secondary_events, (s) => s)),
      whenPanel(b, d.published_at),
      wherePanel(b.where || []),
      whoPanel(b.who || []),
      el("div", { class: "panel wide" }, claimsPanel(b.claims || [])),
      collapsible("Why", [note("Why"), why]),
      collapsible("How", [note("How"), b.how ? el("p", {}, b.how) : notStated()]),
      collapsible("Impact", [note("Impact"), impact]),
      collapsible("Context", [note("Context"), list(b.context, (s) => s)]),
      collapsible("Open questions", [note("Open questions"), list(b.open_questions, (s) => s)]),
      panel("Topics", b.topics?.length ? el("p", {}, b.topics.map((t) => [chip(t), " "])) : notStated()),
      panel("Tone", b.tone && (b.tone.label || b.tone.explanation)
        ? el("p", {}, b.tone.label ? [chip(label(b.tone.label)), " "] : null, b.tone.explanation) : notStated())));
  }).catch((error) => {
    if (!current(token)) return;
    show(true, errorView(error, () => detailView(id, nav), "No briefing exists for this article yet."));
  });
}

// --- routing ------------------------------------------------------------------------

function route() {
  // The skip link targets #main: it is not a route.
  if (location.hash === "#main") { main().focus(); return; }
  const token = ++nav;
  const hash = location.hash;
  const m = /^#\/article\/([1-9]\d{0,18})$/.exec(hash);
  if (m) detailView(m[1], token);
  else if (hash === "" || hash === "#" || hash === "#/") listView(token);
  else { ui = null; show(true, notFoundView("This page does not exist.")); }
}

window.addEventListener("hashchange", route);
route();
