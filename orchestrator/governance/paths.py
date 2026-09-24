"""URL path normalisation for tier rules (FR-500, S3.1).

A site marks paths Tier 1 ("/legal/*"). The request tells us which path the
text came from, so the request controls the input to that match. Anything that
normalises one way here and a different way on the site's own server is a way
to read Tier 1 text at Tier 2 -- machine-translated legal text served to a
citizen, which is the failure FR-510 exists to prevent.

So this is deliberately strict rather than clever: normalise the unambiguous
cases, and return ``None`` for everything else. ``None`` means Tier 1 at the
call site, never "no rule matched". Refusing to guess costs an unnecessary
tier_blocked; guessing wrong costs a wrong translation of legal text.

Matching is case-insensitive. Many servers treat paths case-insensitively, and
because ``resolve_tier`` takes the strictest of all matches, folding case can
only add matches and therefore only tighten the result.
"""

from __future__ import annotations

from urllib.parse import unquote

MAX_PATH_LENGTH = 512


def normalise_path(raw: object) -> str | None:
    """A lowercase absolute path with no traversal, or None if it cannot be trusted.

    None is returned for: a non-string, an over-long path, an invalid or double
    percent-encoding, control characters, or traversal that escapes the root.
    """
    if not isinstance(raw, str) or not raw or len(raw) > MAX_PATH_LENGTH:
        return None

    # The path is for matching rules, not for fetching: the query string and
    # fragment are not part of it.
    path = raw.split("?", 1)[0].split("#", 1)[0]

    try:
        path = unquote(path, encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError):
        return None
    # Any '%' surviving one decode pass is either double encoding ("%252e") or a
    # malformed escape ("%2f" with a truncated neighbour, which `unquote` passes
    # through silently). Both are read differently by different servers, and we
    # decode only once by design, so the path cannot be trusted. A genuine
    # literal '%' in a path is refused too: that costs an unnecessary Tier 1,
    # which is the direction we want to be wrong in.
    if "%" in path:
        return None

    # A backslash is a separator on some stacks and a literal on others. It is
    # never needed in a government URL path, so refuse it rather than pick.
    if "\\" in path:
        return None
    if any(ch < " " or ch == "\x7f" for ch in path):
        return None

    if not path.startswith("/"):
        path = "/" + path

    out: list[str] = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if not out:
                return None  # escapes the root: the site would not serve this
            out.pop()
            continue
        out.append(segment)

    return "/" + "/".join(out).lower()


def path_matches(pattern: str, path: str) -> bool:
    """Exact path, or a "/prefix/*" pattern covering the prefix and everything under it.

    No regular expressions: these rules are read and approved by the agency that
    owns the site, and a pattern language they cannot check is worse than a
    narrow one. ``path`` must already be normalised.
    """
    pattern = pattern.lower().rstrip()
    if pattern.endswith("/*"):
        prefix = pattern[:-2] or "/"
        return path == prefix or path.startswith(prefix.rstrip("/") + "/")
    if pattern.endswith("*"):  # "/legal*" also covers "/legalese"; the site asked for that
        return path.startswith(pattern[:-1])
    return path == pattern
