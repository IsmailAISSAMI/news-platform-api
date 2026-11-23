"""robots.txt parsing per RFC 9309, plus the non-standard Crawl-delay.

Why not `urllib.robotparser`: it does not support `*` / `$` wildcards (so Hespress's
`Disallow: /*?print` would be matched literally) and it drops rules that appear before
any `User-agent` line. We deliberately apply such orphan rules to everyone: following a
publisher's evident intent is the conservative choice.
"""

import re
from dataclasses import dataclass, field
from functools import cached_property, lru_cache


@dataclass(frozen=True, slots=True)
class Rule:
    allow: bool
    pattern: str

    @property
    def specificity(self) -> int:
        return len(self.pattern)

    def matches(self, path: str) -> bool:
        return _compile(self.pattern).match(path) is not None


@lru_cache(maxsize=512)
def _compile(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = ".*".join(re.escape(part) for part in body.split("*"))
    return re.compile(regex + ("$" if anchored else ""))


@dataclass(frozen=True)
class RobotsPolicy:
    rules: tuple[Rule, ...] = ()
    crawl_delay_s: float | None = None
    disallow_all: bool = False
    sitemaps: tuple[str, ...] = field(default=())

    @classmethod
    def allow_everything(cls) -> "RobotsPolicy":
        return cls()

    @classmethod
    def deny_everything(cls) -> "RobotsPolicy":
        return cls(disallow_all=True)

    @cached_property
    def _ordered(self) -> tuple[Rule, ...]:
        # Longest (most specific) match wins; on equal length, Allow wins (RFC 9309 §2.2.2).
        return tuple(sorted(self.rules, key=lambda r: (r.specificity, r.allow), reverse=True))

    def can_fetch(self, path: str) -> bool:
        """`path` is the URL path plus query, e.g. "/feed?paged=2"."""
        if self.disallow_all:
            return False
        if path == "/robots.txt":
            return True
        for rule in self._ordered:
            if rule.matches(path):
                return rule.allow
        return True


@dataclass
class _Group:
    agents: list[str] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    crawl_delay_s: float | None = None


def parse_robots(text: str, agent: str) -> RobotsPolicy:
    """Rules that apply to `agent`, our robots product token (e.g. "news-platform-api").

    Group selection (RFC 9309 §2.2.1): groups naming our product token, merged; otherwise
    all `*` groups, merged. Orphan rules (before any User-agent line) always apply.
    """
    token = agent.lower()
    groups: list[_Group] = []
    orphan = _Group()
    current: _Group | None = None
    previous_was_agent = False
    sitemaps: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()

        if key == "user-agent":
            if current is None or not previous_was_agent:
                current = _Group()
                groups.append(current)
            current.agents.append(value.lower())
            previous_was_agent = True
            continue
        previous_was_agent = False
        target = current if current is not None else orphan

        if key in {"allow", "disallow"}:
            if value:  # an empty Disallow means "nothing disallowed"
                target.rules.append(Rule(allow=key == "allow", pattern=value))
        elif key == "crawl-delay":
            delay = _parse_delay(value)
            if delay is not None:
                target.crawl_delay_s = max(target.crawl_delay_s or 0.0, delay)
        elif key == "sitemap":
            sitemaps.append(value)

    specific = [g for g in groups if token in g.agents]
    selected = specific or [g for g in groups if "*" in g.agents]
    applicable = [*selected, orphan]

    delays = [g.crawl_delay_s for g in applicable if g.crawl_delay_s is not None]
    return RobotsPolicy(
        rules=tuple(rule for g in applicable for rule in g.rules),
        crawl_delay_s=max(delays) if delays else None,
        sitemaps=tuple(sitemaps),
    )


def _parse_delay(value: str) -> float | None:
    try:
        delay = float(value)
    except ValueError:
        return None
    return delay if delay >= 0 else None
