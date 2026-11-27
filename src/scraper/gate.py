"""The Content Gate: decides whether a fetched page is real content.

Deterministic: the same page and config always get the same Verdict. Thresholds come
from config; the markers below are code, and each one should be backed by a page in
the Content Gate corpus (tests/corpus).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from .config import GateConfig
from .page import FetchedPage

Decision = Literal["accepted", "suspect", "rejected"]

# Reasons a Verdict gives. A CHALLENGE is non-interactive and worth polling; an
# INTERACTIVE_CHALLENGE (Turnstile, CAPTCHA) is never attempted locally. A non-2xx
# status gives "http_<code>".
OK = "ok"
THIN = "thin"
CHALLENGE = "challenge"
INTERACTIVE_CHALLENGE = "interactive_challenge"
COOKIE_WALL = "cookie_wall"
SOFT_404 = "soft_404"
BLOCKED = "blocked"
EMPTY_SHELL = "empty_shell"
TOO_SHORT = "too_short"
FETCH_ERROR = "fetch_error"


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    reason: str  # one of the reasons above, or "http_<code>"
    detail: str = ""


@dataclass(frozen=True)
class _Marker:
    text: str  # matched against the lower-cased HTML
    strong: bool  # only ever on challenge pages; weak ones also turn up on real pages
    interactive: bool  # a human has to act, so polling is pointless


_CHALLENGE_MARKERS = (
    _Marker("_cf_chl_opt", strong=True, interactive=False),  # Cloudflare challenge bootstrap
    _Marker("/cdn-cgi/challenge-platform/h/", strong=True, interactive=False),  # its orchestrator
    _Marker("ctype:'interactive'", strong=True, interactive=True),
    _Marker("captcha-delivery.com", strong=True, interactive=True),  # DataDome captcha
    _Marker("px-captcha", strong=True, interactive=True),  # PerimeterX "Press & Hold"
    # Weak: also seen on real pages (a login form with reCAPTCHA), so they count only
    # when the page has little text. The bare "/cdn-cgi/challenge-platform/" path is
    # not a marker at all: Cloudflare injects its bot-management script from there
    # into ordinary pages (corpus: medium_404).
    _Marker("challenges.cloudflare.com/turnstile", strong=False, interactive=True),
    _Marker("cf-turnstile", strong=False, interactive=True),
    _Marker("g-recaptcha", strong=False, interactive=True),
    _Marker("recaptcha/api.js", strong=False, interactive=True),
    _Marker("h-captcha", strong=False, interactive=True),
    _Marker("hcaptcha.com", strong=False, interactive=True),
    _Marker("press &amp; hold", strong=False, interactive=True),
    _Marker("press & hold", strong=False, interactive=True),
    _Marker("_pxappid", strong=False, interactive=False),
    _Marker("checking your browser", strong=False, interactive=False),
    _Marker("checking if the site connection is secure", strong=False, interactive=False),
    _Marker("verify you are human", strong=False, interactive=False),
)
_CHALLENGE_TITLES = ("just a moment", "attention required! | cloudflare")


def _challenge(page: FetchedPage, html: str, thin: bool) -> Verdict | None:
    found: list[str] = []
    if page.headers.get("cf-mitigated", "").lower() == "challenge":
        found.append("cf-mitigated header")
    if "x-datadome" in page.headers and page.status in (401, 403):
        found.append("DataDome block")
    title = (page.title or "").strip().lower()
    found += [f"title '{page.title}'" for t in _CHALLENGE_TITLES if title.startswith(t)]
    present = [m for m in _CHALLENGE_MARKERS if m.text in html]
    found += [m.text for m in present if m.strong or thin]
    if not found:
        return None
    interactive = [m.text for m in present if m.interactive]
    if interactive:
        return Verdict("rejected", INTERACTIVE_CHALLENGE, ", ".join(interactive))
    return Verdict("rejected", CHALLENGE, ", ".join(found))


# Consent-wall language. Extraction normally drops cookie banners, so when several of
# these survive into a thin page's main content, the main content is the wall.
_CONSENT_PHRASES = (
    "before you continue",
    "we value your privacy",
    "accept all",
    "reject all",
    "cookie preferences",
    "cookie settings",
    "manage cookies",
    "use cookies",
    "our partners",
    "consent to",
    "privacy settings",
)


def _cookie_wall(page: FetchedPage, thin: bool) -> Verdict | None:
    if not thin:
        return None
    text = f"{page.title or ''}\n{page.markdown}".lower()
    found = [p for p in _CONSENT_PHRASES if p in text]
    if len(found) >= 3:
        return Verdict("rejected", COOKIE_WALL, ", ".join(found))
    return None


# "Not found" and block pages served with a 200. Both title patterns and phrases count
# only on thin pages: a full article can be titled "404 Not Found" (corpus: mdn_404_article).
_SOFT_404_PHRASES = (
    "page not found",
    "can't find that page",
    "cannot find that page",
    "can't find the page",
    "couldn't find the page",
    "could not find the page",
    "page you are looking for",
    "page you were looking for",
    "page you requested",
    "doesn't exist",
    "does not exist",
    "no longer available",
    "nothing was found",
    "error 404",
)
_SOFT_404_TITLES = re.compile(
    r"^(error\s*)?404\b|^page not found|\bnot found\s*$|^oops", re.IGNORECASE
)
_BLOCKED_TITLES = re.compile(
    r"^access denied|^access to this page has been denied|^403 forbidden|^forbidden\s*$"
    r"|^request blocked|^you have been blocked",
    re.IGNORECASE,
)


def _title_or_soft_404(page: FetchedPage, thin: bool) -> Verdict | None:
    if not thin:
        return None
    title = (page.title or "").strip()
    if _BLOCKED_TITLES.search(title):
        return Verdict("rejected", BLOCKED, f"title '{title}'")
    if _SOFT_404_TITLES.search(title):
        return Verdict("rejected", SOFT_404, f"title '{title}'")
    text = page.markdown.lower()
    found = [p for p in _SOFT_404_PHRASES if p in text]
    if found:
        return Verdict("rejected", SOFT_404, ", ".join(found))
    return None


# Signs that a page with almost no text is a JavaScript app waiting to render.
_SHELL_MARKERS = (
    'id="root"',
    'id="app"',
    'id="__next"',
    'id="__nuxt"',
    'id="svelte"',
    "ng-version",
    "enable javascript",
    "requires javascript",
)


def judge(page: FetchedPage, config: GateConfig) -> Verdict:
    html = page.html.lower()
    length = len(page.markdown)
    thin = length < config.accepted_chars

    challenge = _challenge(page, html, thin)
    if challenge:
        return challenge
    if not 200 <= page.status < 300:
        return Verdict("rejected", f"http_{page.status}", f"status {page.status}")
    for check in (_cookie_wall(page, thin), _title_or_soft_404(page, thin)):
        if check:
            return check
    if length < config.min_chars:
        shell = [m for m in _SHELL_MARKERS if m in html]
        if shell and "<script" in html:
            return Verdict("rejected", EMPTY_SHELL, f"{length} chars of text; {', '.join(shell)}")
        return Verdict("rejected", TOO_SHORT, f"{length} chars of text")
    if thin:
        return Verdict("suspect", THIN, f"{length} chars of text")
    return Verdict("accepted", OK)
