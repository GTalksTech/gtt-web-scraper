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

# Reasons for a rejected Verdict. A "challenge" is non-interactive and worth polling;
# an "interactive_challenge" (Turnstile, CAPTCHA) is never attempted locally.
CHALLENGE = "challenge"
INTERACTIVE_CHALLENGE = "interactive_challenge"


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    reason: str  # short machine-readable cause, e.g. "challenge", "too_short"
    detail: str = ""


# Markers that only ever appear on challenge or block pages.
_STRONG_CHALLENGE_MARKERS = (
    "_cf_chl_opt",  # Cloudflare challenge bootstrap
    "/cdn-cgi/challenge-platform/h/",  # Cloudflare challenge orchestrator
    "captcha-delivery.com",  # DataDome block/captcha iframe
    "px-captcha",  # PerimeterX block
)
_CHALLENGE_TITLES = ("just a moment", "attention required! | cloudflare")

# Markers that also turn up on real pages (a login form with reCAPTCHA, Cloudflare's
# bot-management script), so they count only when the page has little text.
_WEAK_CHALLENGE_MARKERS = (
    # Not the bare "/cdn-cgi/challenge-platform/" path: Cloudflare injects its
    # bot-management script from there into ordinary pages (corpus: indeed_home).
    "challenges.cloudflare.com/turnstile",
    "cf-turnstile",
    "g-recaptcha",
    "recaptcha/api.js",
    "h-captcha",
    "hcaptcha.com",
    "_pxappid",
    "press &amp; hold",
    "press & hold",
    "checking your browser",
    "checking if the site connection is secure",
    "verify you are human",
)

# Any of these on a challenge page means a human has to act: never poll it.
_INTERACTIVE_MARKERS = (
    "challenges.cloudflare.com/turnstile",
    "cf-turnstile",
    "ctype:'interactive'",
    "g-recaptcha",
    "recaptcha/api.js",
    "h-captcha",
    "hcaptcha.com",
    "captcha-delivery.com",
    "px-captcha",
    "press &amp; hold",
    "press & hold",
)


def _challenge(page: FetchedPage, html: str, thin: bool) -> Verdict | None:
    found: list[str] = []
    if page.headers.get("cf-mitigated", "").lower() == "challenge":
        found.append("cf-mitigated header")
    if "x-datadome" in page.headers and page.status in (401, 403):
        found.append("DataDome block")
    title = (page.title or "").strip().lower()
    found += [f"title '{page.title}'" for t in _CHALLENGE_TITLES if title.startswith(t)]
    found += [m for m in _STRONG_CHALLENGE_MARKERS if m in html]
    if thin:
        found += [m for m in _WEAK_CHALLENGE_MARKERS if m in html]
    if not found:
        return None
    interactive = [m for m in _INTERACTIVE_MARKERS if m in html]
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


def _cookie_wall(page: FetchedPage, thin: bool) -> Verdict | None:
    if not thin:
        return None
    text = f"{page.title or ''}\n{page.markdown}".lower()
    found = [p for p in _CONSENT_PHRASES if p in text]
    if len(found) >= 3:
        return Verdict("rejected", "cookie_wall", ", ".join(found))
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
        return Verdict("rejected", "blocked", f"title '{title}'")
    if _SOFT_404_TITLES.search(title):
        return Verdict("rejected", "soft_404", f"title '{title}'")
    text = page.markdown.lower()
    found = [p for p in _SOFT_404_PHRASES if p in text]
    if found:
        return Verdict("rejected", "soft_404", ", ".join(found))
    return None


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
            return Verdict("rejected", "empty_shell", f"{length} chars of text; {', '.join(shell)}")
        return Verdict("rejected", "too_short", f"{length} chars of text")
    if thin:
        return Verdict("suspect", "thin", f"{length} chars of text")
    return Verdict("accepted", "ok")
