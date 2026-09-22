"""Verify the URLs in docstrings and docs still resolve.

A dead link in a cookbook docstring is worse than no link: the script is a
reference someone reads before writing their own code, and the citation is how
they check the claim. Three had rotted by 2026-09-22 —
`book.anchor-lang.com/anchor_bts/discriminator.html` (Anchor restructured its
docs), `docs.chainstack.com/quickstart/` and `docs.chainstack.com/docs/trader-nodes`.

Offline by default, so this can run in the normal set:

  A. Every URL is well-formed and uses https, apart from the deliberate
     unroutable stubs the offline verifiers point their fake clients at.
  B. No URL matches one of the dead endpoints already removed, so they cannot
     come back by copy-paste.

With `--live` it fetches every one and fails on anything >= 400. That needs
network and takes about a minute, so it is opt-in.

Usage:
    uv run tests/regression/verify_documentation_links.py
    uv run tests/regression/verify_documentation_links.py --live
"""

import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[2]

SEARCH_ROOTS = ("cookbook", "tools", "tests", "src", "bots")
SEARCH_SUFFIXES = {".py", ".md", ".yaml"}
EXTRA_FILES = ("README.md", "CLAUDE.md")

URL = re.compile(r"https?://[^\s'\"()<>\]`,]+")

# Hosts that are not meant to resolve. The offline verifiers point a stub client
# at unroutable addresses on purpose, and example.com is the domain RFC 2606
# reserves for exactly the placeholder metadata URI a test coin is created with.
STUB_HOSTS = {
    "127.0.0.1",
    "dummy",
    "offline.invalid",
    "stub.invalid",
    "example.com",
}

# Endpoints that used to be cited here and no longer exist. Keeping them listed
# means a stale copy-paste fails loudly instead of shipping.
DEAD = {
    "https://book.anchor-lang.com/anchor_bts/discriminator.html",
    "https://www.anchor-lang.com/anchor_bts/discriminator.html",
    "https://docs.chainstack.com/quickstart/",
    "https://docs.chainstack.com/docs/trader-nodes",
}

HTTP_ERROR = 400
TIMEOUT_SECONDS = 25


def collect_urls() -> dict[str, list[str]]:
    """Find every URL in the repo's docs and docstrings.

    Returns:
        Each URL mapped to the `path:line` locations that mention it
    """
    files = [
        p
        for root in SEARCH_ROOTS
        for p in (PROJECT_ROOT / root).rglob("*")
        if p.is_file()
        and p.suffix in SEARCH_SUFFIXES
        # This file lists the dead endpoints on purpose; scanning it would
        # report its own registry as a finding.
        and p.resolve() != Path(__file__).resolve()
    ]
    files += [PROJECT_ROOT / name for name in EXTRA_FILES]

    found: dict[str, list[str]] = {}
    for path in files:
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        for match in URL.finditer(text):
            url = match.group(0).rstrip(".,;:")
            line = text[: match.start()].count("\n") + 1
            found.setdefault(url, []).append(f"{path.relative_to(PROJECT_ROOT)}:{line}")
    return found


def is_checkable(url: str) -> bool:
    """Whether a URL is a real address rather than a stub or a format template.

    Args:
        url: The URL to classify

    Returns:
        True if it should resolve to a live page
    """
    if "{" in url:  # f-string template, e.g. an explorer link
        return False
    host = urlparse(url).hostname or ""
    return host not in STUB_HOSTS


def check_urls_well_formed(found: dict[str, list[str]]) -> None:
    """Every real URL must be https with a hostname."""
    offenders = []
    for url, where in sorted(found.items()):
        if not is_checkable(url):
            continue
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            offenders.append(f"{url}  ({where[0]})")
    assert not offenders, "URLs that are not https with a host:\n  " + "\n  ".join(
        offenders
    )


def check_no_known_dead(found: dict[str, list[str]]) -> None:
    """None of the endpoints already found dead may reappear."""
    offenders = [
        f"{url}  ({', '.join(where)})"
        for url, where in sorted(found.items())
        if url in DEAD
    ]
    assert not offenders, "known-dead URLs are back:\n  " + "\n  ".join(offenders)


def check_urls_resolve(found: dict[str, list[str]]) -> None:
    """Fetch every real URL and fail on anything >= 400.

    Args:
        found: URLs mapped to where they appear

    Raises:
        AssertionError: If any URL returns an error status or cannot be reached
    """
    offenders = []
    targets = sorted(url for url in found if is_checkable(url))
    print(f"  fetching {len(targets)} URLs...")
    for url in targets:
        request = urllib.request.Request(  # noqa: S310
            url, method="GET", headers={"User-Agent": "Mozilla/5.0 (link-check)"}
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        except Exception as exc:  # noqa: BLE001
            offenders.append(f"{url}  unreachable: {exc}  ({found[url][0]})")
            continue
        if status >= HTTP_ERROR:
            offenders.append(f"{url}  HTTP {status}  ({found[url][0]})")
    assert not offenders, "dead links:\n  " + "\n  ".join(offenders)


def main() -> None:
    """Run the checks and report."""
    live = "--live" in sys.argv

    print("=" * 72)
    print("Verifying documentation links")
    print("=" * 72)

    found = collect_urls()
    checkable = [u for u in found if is_checkable(u)]
    print(f"{len(found)} distinct URLs, {len(checkable)} of them real addresses\n")

    check_urls_well_formed(found)
    print("every URL is https with a host -> OK")
    check_no_known_dead(found)
    print("no known-dead URL has come back -> OK")

    if live:
        check_urls_resolve(found)
        print("every URL resolves -> OK")
    else:
        print("\n(skipping the network check; pass --live to fetch each URL)")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
