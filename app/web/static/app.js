// Briefing screen: list and detail views over the read-only JSON API (same origin).
import {
  chip, collapsible, el, formatDate, formatPublished, label, list, notStated, NOT_STATED,
} from "./dom.js";

const API = "/api/v1";
const PAGE = 20;
const main = () => document.getElementById("main");

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

function show(...nodes) {
  const root = main();
  // Skip empty slots (e.g. no review banner): replaceChildren would print "null".
  root.replaceChildren(...nodes.filter((n) => n !== null && n !== undefined));
  root.focus({ preventScroll: true });
}

function errorView(error, retry) {
  if (error instanceof HttpError && error.status === 404) {
    return el("div", { class: "banner error", role: "alert" }, "No analysis for this article yet. ",
      el("a", { href: "#/" }, "Back to briefings"));
  }
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
  return el("li", { class: "card" },
    el("div", { class: "meta" },
      el("time", { datetime: item.published_at }, formatPublished(item.published_at)),
      statusChips(item.report_type, item.event_status, item.review_required)),
    el("h2", {}, el("a", { href: `#/article/${item.article_id}` }, item.headline || item.title)),
    el("div", { class: "facts" },
      el("span", {}, "When: ", formatDate(item.event_date) || "date not stated"),
      el("span", {}, "Where: ", places.length ? places.join(", ") : "not stated"),
      el("span", {}, "Who: ", actors.length ? actors.join(", ") : "not stated")));
}

function matches(item) {
  if (state.status && item.event_status !== state.status) return false;
  if (!state.query) return true;
  const hay = [item.headline, item.title, ...(item.places || []).map((p) => p.name_as_written),
    ...(item.actors || []).map((a) => a.full_name)].join(" ").toLowerCase();
  return hay.includes(state.query);
}

function renderList() {
  const statuses = [...new Set(state.items.map((i) => i.event_status).filter(Boolean))].sort();
  const search = el("input", {
    type: "search", "aria-label": "Search loaded briefings", placeholder: "Search loaded briefings",
    value: state.query,
  });
  const select = el("select", { "aria-label": "Filter by event status" },
    el("option", { value: "" }, "All statuses"),
    statuses.map((s) => el("option", { value: s, selected: s === state.status }, label(s))));
  const cards = el("ul", { class: "cards", "aria-live": "polite" });
  const count = el("p", { class: "muted", role: "status" });
  const more = state.cursor
    ? el("button", { type: "button", onclick: () => loadMore(more) }, "Load more")
    : null;

  const paint = () => {
    const visible = state.items.filter(matches);
    cards.replaceChildren(...visible.map(card));
    count.textContent = `${visible.length} of ${state.items.length} loaded briefing(s) shown.`;
  };
  search.addEventListener("input", () => { state.query = search.value.trim().toLowerCase(); paint(); });
  select.addEventListener("change", () => { state.status = select.value; paint(); });
  paint();

  show(el("h1", {}, "Latest briefings"),
    el("div", { class: "toolbar" }, search, select), count, cards, more);
  if (state.items.length === 0) cards.replaceChildren(el("li", { class: "muted" }, "No briefings yet."));
}

async function loadMore(button) {
  if (button) { button.disabled = true; button.textContent = "Loading..."; }
  try {
    const query = `?limit=${PAGE}` + (state.cursor ? `&cursor=${encodeURIComponent(state.cursor)}` : "");
    const page = await getJson(`/briefings${query}`);
    state.items.push(...page.items);
    state.cursor = page.next_cursor;
    state.loaded = true;
    renderList();
  } catch (error) {
    show(errorView(error, () => loadMore()));
  }
}

function listView() {
  if (state.loaded) renderList();
  else loadMore();
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

function panel(title, ...content) {
  return el("section", { class: "panel", "aria-label": title }, el("h2", {}, title), ...content);
}

// A date counts as "happened" when it is on or before publication, at its own precision.
function happened(date, published) {
  const pub = published.slice(0, 10);
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
    list(items, (a) => el("span", { class: `actor t-${String(a.type || "other").toLowerCase()}` },
      a.full_name || a.name_as_written,
      a.role ? el("span", { class: "attr" }, ` (${a.role}; ${label(a.type)})`) : null))]));
}

function claimsPanel(claims) {
  if (!claims.length) return panel("Claims", notStated());
  return panel("Claims", el("ul", { class: "cards" }, claims.map((c) => el("li",
    { class: `claim k-${c.kind}` },
    chip(c.kind === "allegation" ? "allegation, not a finding" : label(c.kind), c.kind), " ",
    c.text,
    c.attributed_to ? el("div", { class: "attr" }, `according to ${c.attributed_to}`) : null))));
}

function detailView(id) {
  show(el("p", { class: "muted" }, "Loading briefing..."));
  getJson(`/articles/${encodeURIComponent(id)}/briefing`).then((d) => {
    const b = d.briefing;
    const o = b.overview || {};
    document.title = `${o.headline || d.title} - Briefing`;
    const header = el("header", {},
      el("a", { href: "#/" }, "Back to briefings"),
      el("h1", {}, o.headline || d.title),
      el("div", { class: "meta" },
        el("span", {}, d.source?.name || ""),
        el("time", { datetime: d.published_at }, `Published ${formatPublished(d.published_at)}`),
        statusChips(o.report_type, o.event_status, d.review_required)),
      el("a", { class: "origin", href: d.source_url, target: "_blank", rel: "noopener noreferrer" },
        `Open original article (${d.source?.name || "source"})`));
    const banner = d.review_required ? el("div", { class: "banner", role: "note" },
      `${d.removed_items} item(s) removed: their quotes could not be verified in the article.`) : null;
    const why = list(b.why, (c) => [c.text, c.attributed_to
      ? el("span", { class: "attr" }, ` (according to ${c.attributed_to})`) : null]);
    const impact = list(b.impact, (i) => [el("strong", {}, `${label(i.area)}: `), i.text, " ",
      chip(label(i.certainty))]);
    show(header, banner, el("div", { class: "grid" },
      panel("What", b.what?.main_event ? el("p", {}, b.what.main_event) : notStated(),
        el("h3", {}, "Secondary events"), list(b.what?.secondary_events, (s) => s)),
      whenPanel(b, d.published_at),
      wherePanel(b.where || []),
      whoPanel(b.who || []),
      el("div", { class: "panel wide" }, claimsPanel(b.claims || [])),
      collapsible("Why", why),
      collapsible("How", b.how ? el("p", {}, b.how) : notStated()),
      collapsible("Impact", impact),
      collapsible("Context", list(b.context, (s) => s)),
      collapsible("Open questions", list(b.open_questions, (s) => s)),
      panel("Topics", b.topics?.length ? el("p", {}, b.topics.map((t) => [chip(t), " "])) : notStated()),
      panel("Tone", b.tone ? el("p", {}, chip(label(b.tone.label)), " ", b.tone.explanation) : notStated())));
  }).catch((error) => show(errorView(error, () => detailView(id))));
}

// --- routing ------------------------------------------------------------------------

function route() {
  const m = /^#\/article\/(\d+)$/.exec(location.hash);
  if (m) detailView(m[1]);
  else { document.title = "News briefings"; listView(); }
}

window.addEventListener("hashchange", route);
route();
