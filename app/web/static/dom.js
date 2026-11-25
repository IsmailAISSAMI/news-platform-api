// DOM helpers. All data goes in through textContent or attributes: never as HTML.

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  // Flatten fully: panels pass nested arrays (groups of [heading, list]).
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export const chip = (text, cls = "") => el("span", { class: `chip ${cls}`.trim() }, text);

export const label = (value) => String(value || "").replace(/_/g, " ");

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// Show a date at the precision it was given: "2026", "Nov 2026" or "3 Nov 2026".
export function formatDate(value) {
  if (!value) return null;
  const m = /^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?/.exec(String(value));
  if (!m) return String(value);
  const [, y, mo, d] = m;
  if (!mo) return `${y} (year only)`;
  const month = MONTHS[Number(mo) - 1] || mo;
  if (!d) return `${month} ${y} (month only)`;
  return `${Number(d)} ${month} ${y}`;
}

export function formatPublished(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso);
  return date.toLocaleString("en-GB", {
    day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

export const NOT_STATED = "Not stated in the article.";
export const notStated = () => el("p", { class: "muted" }, NOT_STATED);

export function list(items, render) {
  if (!items || items.length === 0) return notStated();
  return el("ul", {}, items.map((item) => el("li", {}, render(item))));
}

let counter = 0;
export function collapsible(title, content, open = false) {
  const id = `sec-${++counter}`;
  const body = el("div", { id, hidden: !open }, content);
  const button = el(
    "button",
    { class: "toggle", type: "button", "aria-expanded": String(open), "aria-controls": id },
    title,
    el("span", { class: "sign", "aria-hidden": "true" }),
  );
  button.addEventListener("click", () => {
    const expanded = button.getAttribute("aria-expanded") === "true";
    button.setAttribute("aria-expanded", String(!expanded));
    body.hidden = expanded;
  });
  return el("section", { class: "panel wide collapsible" }, el("h2", {}, button), body);
}
