"""The static briefing screen: served at /ui with a strict CSP, local-only and XSS-safe."""

import re
from html.parser import HTMLParser
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import UI_DIR

UI_FILES = sorted(p for p in UI_DIR.rglob("*") if p.is_file())
JS_FILES = [p for p in UI_FILES if p.suffix == ".js"]
MAX_TOTAL_BYTES = 60 * 1024


class _Refs(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.refs: list[str] = []
        self.tags: dict[str, dict[str, str | None]] = {}
        self.inline_scripts = 0
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        self.tags.setdefault(tag, attributes)
        for key in ("src", "href"):
            value = attributes.get(key)
            if value and not value.startswith("#") and (tag != "a"):
                self.refs.append(value)
        if tag == "meta" and attributes.get("name"):
            self.tags[f"meta:{attributes['name']}"] = attributes
        self._in_script = tag == "script"

    def handle_endtag(self, tag: str) -> None:
        self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script and data.strip():
            self.inline_scripts += 1


def _parse_index() -> _Refs:
    parser = _Refs()
    parser.feed((UI_DIR / "index.html").read_text(encoding="utf-8"))
    return parser


def _strip_comments(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if path.suffix in {".js", ".css"}:
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    if path.suffix == ".js":
        text = re.sub(r"(?m)^\s*//.*$", "", text)
    if path.suffix == ".html":
        text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    return text


def test_index_is_served_with_csp(client: TestClient) -> None:
    response = client.get("/ui/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "'unsafe-inline'" not in csp
    assert "'unsafe-eval'" not in csp
    assert response.headers["x-content-type-options"] == "nosniff"


def test_api_responses_carry_no_ui_csp(client: TestClient) -> None:
    assert "content-security-policy" not in client.get("/health/live").headers


def test_every_referenced_asset_is_served(client: TestClient) -> None:
    refs = _parse_index().refs
    for path in JS_FILES:
        refs += re.findall(r"""from\s+["']\./([^"']+)["']""", path.read_text(encoding="utf-8"))

    assert refs
    for ref in refs:
        assert (UI_DIR / ref).is_file(), ref
        response = client.get(f"/ui/{ref}")
        assert response.status_code == 200, ref
        assert "content-security-policy" in response.headers


def test_ui_is_read_only(client: TestClient) -> None:
    assert client.post("/ui/index.html").status_code == 405


def test_index_has_lang_viewport_and_main() -> None:
    parsed = _parse_index()

    assert parsed.tags["html"].get("lang") == "en"
    assert "width=device-width" in (parsed.tags["meta:viewport"].get("content") or "")
    assert "main" in parsed.tags
    assert parsed.inline_scripts == 0


# The SVG namespace is an identifier the browser never fetches; it is the only allowed URL.
SVG_NAMESPACE = 'xmlns="http://www.w3.org/2000/svg"'


def test_no_external_urls_outside_comments() -> None:
    for path in UI_FILES:
        text = _strip_comments(path).replace(SVG_NAMESPACE, "")
        assert not re.search(r"https?://", text), path.name


def test_js_never_parses_strings_as_code_or_html() -> None:
    forbidden = re.compile(
        r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\s*\(|new\s+Function"
    )
    for path in JS_FILES:
        assert not forbidden.search(_strip_comments(path)), path.name


def test_ui_assets_stay_small() -> None:
    assert sum(p.stat().st_size for p in UI_FILES) < MAX_TOTAL_BYTES


def test_ui_files_are_always_revalidated(client: TestClient) -> None:
    """Stale cached JS modules once hid a UI fix: /ui responses must say no-cache."""
    response = client.get("/ui/app.js")

    assert response.headers["Cache-Control"] == "no-cache"


def test_api_data_is_always_revalidated() -> None:
    """A reclassified analysis was once shown from a stale cached JSON response."""
    app_js = (UI_DIR / "app.js").read_text()

    assert 'cache: "no-cache"' in app_js
