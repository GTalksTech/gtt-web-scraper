"""The GitHub Site Adapter: repos, issues and pull requests, releases and files, from
GitHub's REST API (with GITHUB_API_TOKEN when it is set).

Handled github.com URLs:
  /{owner}/{repo}                          the repo and its README
  /{owner}/{repo}/issues/{n}, /pull/{n}    the issue or pull request and its comments
  /{owner}/{repo}/releases                 the ten newest releases
  /{owner}/{repo}/releases/latest          the newest release
  /{owner}/{repo}/releases/tag/{tag}       one release
  /{owner}/{repo}/blob/{ref}/{path}        one file (a ref containing "/" isn't
                                           recognised; the API's 404 sends it to the Ladder)
Anything else on github.com goes to the Ladder.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urlsplit

from ..config import AdaptersConfig
from ..fetchers import FetchError
from ..page import FetchedPage
from . import adapter_page, get, get_json, iso_date

_HOSTS = ("github.com", "www.github.com")

# First path segments on github.com that are GitHub's own pages, not an owner.
_RESERVED = {
    "about", "apps", "collections", "customer-stories", "enterprise", "events", "explore",
    "features", "login", "marketplace", "new", "notifications", "orgs", "organizations",
    "pricing", "pulls", "issues", "search", "security", "settings", "site", "sponsors",
    "topics", "trending", "users", "codespaces", "readme", "team", "solutions", "resources",
}  # fmt: skip

_JSON = "application/vnd.github+json"
_RAW = "application/vnd.github.raw+json"

# Files shown as they are; any other file goes in a code fence named by its extension.
_MARKDOWN_SUFFIXES = {".md", ".markdown", ".mdx", ".txt", ".rst", ""}


def _route(url: str) -> tuple[str, str, list[str]] | None:
    """(owner, repo, the rest of the path) for a URL this adapter handles."""
    parts = urlsplit(url)
    if (parts.hostname or "").lower() not in _HOSTS:
        return None
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2 or segments[0].lower() in _RESERVED:
        return None
    owner, repo, rest = segments[0], segments[1].removesuffix(".git"), segments[2:]
    if not rest:
        return owner, repo, rest
    kind = rest[0]
    if kind in ("issues", "pull") and len(rest) >= 2 and rest[1].isdigit():
        return owner, repo, rest
    if kind == "releases" and (
        len(rest) == 1
        or (len(rest) == 2 and rest[1] == "latest")
        or (len(rest) >= 3 and rest[1] == "tag")
    ):
        return owner, repo, rest
    if kind == "blob" and len(rest) >= 3:
        return owner, repo, rest
    return None


class GitHubAdapter:
    name = "github"

    def __init__(self, config: AdaptersConfig) -> None:
        self._config = config

    def handles(self, url: str) -> bool:
        return _route(url) is not None

    def fetch(self, url: str) -> FetchedPage:
        route = _route(url)
        if route is None:
            raise FetchError(f"GitHub adapter doesn't handle {url}")
        owner, repo, rest = route
        base = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        if not rest:
            return self._repo(url, base)
        if rest[0] in ("issues", "pull"):
            return self._issue(url, base, rest[1], is_pull=rest[0] == "pull")
        if rest[0] == "releases":
            return self._releases(url, base, f"{owner}/{repo}", rest[1:])
        return self._file(url, base, f"{owner}/{repo}", ref=rest[1], path="/".join(rest[2:]))

    def _repo(self, url: str, base: str) -> FetchedPage:
        repo = self._json(base)
        try:
            readme = self._raw(f"{base}/readme")
        except FetchError:
            readme = ""  # a repo without a README is still a repo
        name = str(repo.get("full_name") or base.removeprefix("/repos/"))
        facts = [
            f"{repo.get('stargazers_count', 0)} stars",
            f"{repo.get('forks_count', 0)} forks",
        ]
        if repo.get("language"):
            facts.append(str(repo["language"]))
        license_id = (repo.get("license") or {}).get("spdx_id")
        if license_id and license_id != "NOASSERTION":
            facts.append(f"{license_id} license")
        if repo.get("archived"):
            facts.append("archived")
        if repo.get("pushed_at"):
            facts.append(f"last pushed {iso_date(repo['pushed_at'])}")
        lines = [f"# {name}", ""]
        if repo.get("description"):
            lines += [str(repo["description"]), ""]
        lines += [" · ".join(facts), ""]
        if repo.get("topics"):
            lines += [f"Topics: {', '.join(repo['topics'])}", ""]
        if repo.get("homepage"):
            lines += [f"Homepage: {repo['homepage']}", ""]
        if readme.strip():
            lines += ["## README", "", readme.strip()]
        return adapter_page(url, "\n".join(lines), name, iso_date(repo.get("pushed_at")))

    def _issue(self, url: str, base: str, number: str, is_pull: bool) -> FetchedPage:
        issue = self._json(f"{base}/issues/{number}")
        comments = self._json(f"{base}/issues/{number}/comments?per_page=100")
        kind = "Pull request" if is_pull or issue.get("pull_request") else "Issue"
        title = f"{issue.get('title', '')} (#{number})"
        meta = [
            f"{kind} in {base.removeprefix('/repos/')}",
            str(issue.get("state", "")),
            f"opened by @{_login(issue)} on {iso_date(issue.get('created_at'))}",
        ]
        labels = [str(label.get("name")) for label in issue.get("labels") or []]
        if labels:
            meta.append(f"labels: {', '.join(labels)}")
        lines = [f"# {title}", "", " · ".join(meta), "", str(issue.get("body") or "").strip()]
        if isinstance(comments, list) and comments:
            lines += ["", f"## Comments ({len(comments)})"]
            for comment in comments:
                lines += [
                    "",
                    f"### @{_login(comment)} on {iso_date(comment.get('created_at'))}",
                    "",
                    str(comment.get("body") or "").strip(),
                ]
        return adapter_page(url, "\n".join(lines), title, iso_date(issue.get("created_at")))

    def _releases(self, url: str, base: str, repo: str, rest: list[str]) -> FetchedPage:
        if not rest:
            releases = self._json(f"{base}/releases?per_page=10")
            if not isinstance(releases, list):
                raise FetchError("GitHub API answered releases with something other than a list")
            lines = [f"# {repo} releases"]
            for release in releases:
                lines += ["", *_release(release, heading="##")]
            published = iso_date(releases[0].get("published_at")) if releases else None
            return adapter_page(url, "\n".join(lines), f"{repo} releases", published)
        path = "/latest" if rest[0] == "latest" else f"/tags/{quote('/'.join(rest[1:]), safe='')}"
        release = self._json(f"{base}/releases{path}")
        title = f"{repo} {release.get('name') or release.get('tag_name')}"
        lines = [f"# {title}", "", *_release(release, heading=None)]
        return adapter_page(url, "\n".join(lines), title, iso_date(release.get("published_at")))

    def _file(self, url: str, base: str, repo: str, ref: str, path: str) -> FetchedPage:
        content = self._raw(f"{base}/contents/{quote(path)}?ref={quote(ref, safe='')}")
        if "\x00" in content:
            raise FetchError(f"{path} is a binary file")
        title = f"{repo}/{path} at {ref}"
        suffix = PurePosixPath(path).suffix.lower()
        if suffix in _MARKDOWN_SUFFIXES:
            body = content.strip()
        else:
            fence = "````" if "```" in content else "```"
            body = f"{fence}{suffix.lstrip('.')}\n{content.rstrip()}\n{fence}"
        return adapter_page(url, f"# {title}\n\n{body}", title, None)

    def _headers(self, accept: str) -> dict[str, str]:
        headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
        if self._config.github_token:
            headers["Authorization"] = f"Bearer {self._config.github_token}"
        return headers

    def _api(self, path: str) -> str:
        return f"{self._config.github_api_url.rstrip('/')}{path}"

    def _json(self, path: str) -> Any:
        return get_json(self._api(path), "GitHub API", self._config.timeout_s, self._headers(_JSON))

    def _raw(self, path: str) -> str:
        response = get(self._api(path), "GitHub API", self._config.timeout_s, self._headers(_RAW))
        return str(response.text)


def _login(item: dict[str, Any]) -> str:
    return str((item.get("user") or {}).get("login") or "ghost")


def _release(release: dict[str, Any], heading: str | None) -> list[str]:
    """A release as markdown lines: optional heading, a facts line, then its notes."""
    name = release.get("name") or release.get("tag_name")
    facts = [f"Tag {release.get('tag_name')}", f"published {iso_date(release.get('published_at'))}"]
    if release.get("prerelease"):
        facts.append("pre-release")
    lines = [f"{heading} {name}", ""] if heading else []
    return [*lines, " · ".join(facts), "", str(release.get("body") or "").strip()]

