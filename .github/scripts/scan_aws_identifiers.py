#!/usr/bin/env python3
"""Scan the lines a pull request adds for AWS account IDs and EC2 instance IDs.

The point of the check is to keep unredacted identifiers out of the repository:
a twelve-digit AWS account ID or an EC2 instance ID committed in a skill, an
eval result, a workflow or a document names a real resource in a real account,
and once it is on ``main`` it is in the public history for good.

**Scope: only the lines the pull request adds.** The diff is read with
``git diff --find-renames --unified=0 <base-ref>...<head-ref>``; only the ``+``
lines of that diff are scanned, and removed lines, context lines and the
``+++``/``---`` file headers are all ignored. Scanning whole files, or the whole
tree, was rejected deliberately: ``main`` already carries real-looking
identifiers — committed eval results, example ARNs in skill documentation, a
handful of values in MCP comments — so a whole-file scan would fail pull
requests for content their authors never wrote, and the only way to go green
would be to clean up someone else's lines. Cleaning those up is a separate
change; this check stops new ones arriving.

``--find-renames`` is part of that scope and not a nicety. Without it a pure
file rename is reported as a deletion plus an addition of every line, so moving
a file that already contains identifiers would fail the check for a change that
added nothing. With it, a pure rename produces no added lines at all.

Two patterns are matched:

* **AWS account ID** — exactly twelve digits with no letter or digit on either
  side, so ``arn:aws:iam::123456789012:role/Example`` matches while a
  thirteen-digit number does not. Two lookalikes are excluded, both of which
  occur in this repository's committed eval output: the last group of a UUID
  (``a618bd73-f5dc-4e6b-b1f4-123412341234``, since a UUID ends in twelve
  hexadecimal characters and about one in 250 of those is all digits), and a
  twelve-digit run inside a longer hexadecimal token (``eni-097816109986f5e1d``
  names a network interface, and the twelve digits in the middle of it name no
  account at all). Only a full ``8-4-4-4-`` hex prefix is skipped, so an
  account ID that merely follows a hyphen — ``stack-1234-111122223333`` — is
  still reported.
* **EC2 instance ID** — ``i-`` followed by either eight hexadecimal characters
  (the old form) or seventeen (the current form), case-insensitive, with no
  other hexadecimal word character on either side.

**Redaction suffixes.** The skill evaluation tool redacts identifiers by
replacing each distinct original with a placeholder plus an index, as in
``012345678901_2`` and ``i-1234567890abcdef0_2``, where the trailing number
tells two different originals apart. A matched identifier therefore has an
optional trailing ``_<digits>`` stripped from it before it is compared against
the allowlist, so the bare placeholder and every indexed form of it are treated
alike. The finding reports the token as written; only the comparison is
normalized.

**Four ways a match is suppressed:**

1. The file being scanned *is* the allowlist. Added lines in
   ``.github/aws-identifier-allowlist.json`` are skipped, so adding an entry to
   the allowlist does not trip the scanner on that very entry.
2. An entry in the allowlist file, which must carry a reason.
3. A twelve-digit run that is a bare JSON number rather than a quoted JSON
   string, in a ``.json`` or ``.jsonl`` file. AWS always writes an account ID
   as a string, so a bare number of that length is a byte count or a similar
   measurement, never an account ID.
4. An ``aws-id-ok: <reason>`` marker on the line. Honored in file types that
   have a comment syntax — ``.md``, ``.yaml``, ``.yml``, ``.py``, ``.sh``,
   ``.ts``, ``.js``, ``.html``, ``.htm``, ``.xml``. JSON has no comment syntax,
   which is why the allowlist is the route for JSON content.

The allowlist-path skip comes first, and the marker is checked last, after the
patterns have run: a line naming the marker but holding no identifier suppresses
nothing, so it is left alone rather than treated as an opt-out.

Exit codes: ``0`` when nothing was found, including a pull request that adds no
lines at all; ``1`` when there are findings; ``2`` when the scan could not be
trusted — a malformed allowlist, an unreadable diff file, or a failing ``git``
command. The gate workflow fails the job on any non-zero code; the split exists
so a contributor can tell a leaked identifier from a broken allowlist.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


# Twelve digits, with two guards against things that merely look like an
# account ID. Both were measured against this repository rather than guessed:
# with neither guard, 35 of the 8660 twelve-digit runs in the tree are not
# account IDs at all.
ACCOUNT_RE = re.compile(
    # No alphanumeric character on either side, rather than no digit. Digits
    # alone let a twelve-digit run inside a longer hex token through, which is
    # how the digits inside `eni-097816109986f5e1d` got reported as an account
    # ID — 24 lines of committed eval output in this repository did that. The
    # same guard covers every other AWS resource id built from a hex blob:
    # subnet, vol, sg, snap, ami. The cost is a twelve-digit account ID written
    # flush against a letter, which no AWS output produces and which did not
    # occur anywhere in the tree.
    r"(?<![0-9A-Za-z])"
    # Not the last group of a UUID. A UUID's final group is twelve characters
    # of hex, so roughly one in 250 is all digits — 11 of them sit in committed
    # eval output here, as in `a618bd73-f5dc-4e6b-b1f4-123412341234`. The guard
    # spells out the whole 8-4-4-4 prefix instead of just rejecting a preceding
    # hyphen, so an account ID that merely follows a hyphen is still reported:
    # `stack-1234-111122223333` matches. Every element is a fixed repetition,
    # which is what lets Python's `re` accept it — the module rejects a
    # variable-width lookbehind, and `grep -E` and ripgrep's default engine
    # reject a lookbehind outright.
    r"(?<![0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-)"
    r"([0-9]{12})"
    # The optional ``_<digits>`` group is the skill evaluation tool's redaction
    # suffix. It is written as a boundary-guarded optional group rather than
    # with ``\b`` on purpose: ``_`` is a word character, so ``\b``-anchored
    # patterns do not match the suffixed form at all, and a suffixed
    # placeholder would slip past the allowlist comparison as if it were a
    # different value. Here the group consumes the suffix, and the suffix is
    # then stripped before the comparison.
    r"(?:_([0-9]+))?"
    # Rejects a thirteenth digit, so a longer run of digits never matches a
    # twelve-digit window inside itself and a millisecond timestamp stays
    # unreported.
    r"(?![0-9A-Za-z])"
)

# ``i-`` plus either of the two legal lengths, seventeen hexadecimal characters
# (current) or eight (the old form), with the seventeen-character alternative
# first so the longer match wins.
#
# The left guard is the non-obvious part: without it, ``ami-0abcdef1234567890``
# would match from its ``i-`` and every AMI id in the repository would be
# reported as an instance id. The right guard rejects an eighteenth hexadecimal
# character, so a longer token is not mistaken for a seventeen-character id.
INSTANCE_RE = re.compile(
    r"(?<![0-9A-Za-z])(i-(?:[0-9a-f]{17}|[0-9a-f]{8}))(?:_([0-9]+))?(?![0-9A-Za-z])",
    re.IGNORECASE,
)

ACCOUNT_KIND = "AWS account ID"
INSTANCE_KIND = "EC2 instance ID"

# Per-line opt-out. A line carrying this marker is skipped entirely.
MARKER = "aws-id-ok"
MARKER_WITH_REASON_RE = re.compile(r"aws-id-ok\s*:\s*\S")

# File types where the marker is honored, because a line in them can carry a
# comment. The check is a substring test on the line rather than a parse of the
# language's comment syntax — an approximation, and a generous one: a marker
# inside a string literal in a .py file suppresses that line too. The marker is
# a deliberate opt-out written by the author of the line, so being generous
# about where it sits costs little, while parsing nine languages to be strict
# about it would cost a lot.
COMMENT_BEARING_SUFFIXES = {
    ".md",
    ".yaml",
    ".yml",
    ".py",
    ".sh",
    ".ts",
    ".js",
    ".html",
    ".htm",
    ".xml",
}

# File types where the bare-JSON-number exclusion applies. JSON has no comment
# syntax, so a line in one of these can never carry the marker above, and the
# allowlist is the only suppression route for them.
JSON_SUFFIXES = {".json", ".jsonl"}

# Repository-relative path of the allowlist the gate workflow uses.
DEFAULT_ALLOWLIST = ".github/aws-identifier-allowlist.json"

# Findings are printed and tabulated up to this many; the rest are reported as a
# count. A pull request that adds hundreds of identifiers needs the first screen
# of them and a total, not a wall of annotations.
MAX_PRINTED_FINDINGS = 50

REMEDIATION = (
    "To resolve a finding: redact the value, or add it to "
    f"`{DEFAULT_ALLOWLIST}` with a reason explaining why it is safe to publish, "
    f"or put an `{MARKER}: <reason>` comment on the line itself. The allowlist "
    "is the route for JSON files, which have no comment syntax. See the "
    '"Scanning for AWS Identifiers" section of CONTRIBUTING.md.'
)


@dataclass
class Finding:
    path: str
    line: int
    kind: str
    # The token exactly as it appears on the line, redaction suffix included, so
    # the message matches what the author will search for in the file.
    value: str


def _repo_root() -> Path:
    """Resolve the repository root from this script's location (.github/scripts/)."""
    return Path(__file__).resolve().parents[2]


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo_root, capture_output=True, text=True
    )


def _warn(message: str) -> None:
    """Report a non-fatal problem, as an annotation under Actions."""
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::warning::{message.replace(chr(10), ' ')}")
    else:
        print(f"warning: {message}", file=sys.stderr)


def _error(message: str) -> None:
    """Report a fatal problem, as an annotation under Actions."""
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::error::{message.replace(chr(10), ' ')}")
    else:
        print(f"error: {message}", file=sys.stderr)


def read_diff(repo_root: Path, base_ref: str, head_ref: str) -> str | None:
    """Return the unified diff of what ``head_ref`` adds, or None on failure.

    The flags each earn their place:

    * ``<base_ref>...<head_ref>`` — a three-dot diff, against the merge base, so
      commits that landed on the base branch after the pull request branched are
      not attributed to the pull request.
    * ``--find-renames`` — without it a pure rename is reported as every line
      being added, which would flag identifiers the pull request only moved.
    * ``--unified=0`` — only added lines are scanned, so context lines are
      output nobody reads.
    * ``core.quotePath=false`` — a non-ASCII path is printed as itself in the
      ``+++`` header rather than octal-escaped, which keeps the reported file
      path usable.

    A failing three-dot diff is retried with two dots, which covers the case of
    a shallow clone or an unrelated history where no merge base exists. When
    both fail the caller exits with the configuration failure code: a scan that
    cannot read the diff has to fail loudly rather than report "nothing found".
    """
    flags = (
        "-c",
        "core.quotePath=false",
        "diff",
        "--find-renames",
        "--unified=0",
        "--no-color",
    )

    result = _git(repo_root, *flags, f"{base_ref}...{head_ref}")
    if result.returncode == 0:
        return result.stdout

    _warn(
        f"git diff {base_ref}...{head_ref} failed "
        f"({result.stderr.strip()}); retrying without the merge base"
    )
    result = _git(repo_root, *flags, base_ref, head_ref)
    if result.returncode == 0:
        return result.stdout

    _error(
        f"git diff {base_ref} {head_ref} failed: {result.stderr.strip()}. "
        "The scan cannot run without a diff."
    )
    return None


def read_diff_file(path: str) -> str | None:
    """Read a saved diff from ``path``, or from stdin when ``path`` is ``-``.

    This is the local-testing entry point: with a saved diff the scanner runs no
    ``git`` command at all, which makes it easy to exercise against a fixture.
    """
    if path == "-":
        return sys.stdin.read()
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _error(f"cannot read diff file `{path}`: {exc}")
        return None


def added_lines(diff_text: str) -> list[tuple[str, int, str]]:
    """Parse a unified diff into (path, line number in the new file, line text).

    Only added lines are returned, and the leading ``+`` is stripped from the
    text. The ``+++``/``---`` headers are skipped rather than scanned, so a file
    path that happens to contain a twelve-digit run is never reported as
    content.

    The headers are recognised only outside a hunk. Unified diff is ambiguous
    here: an added line whose own text starts with ``++ `` appears in the diff
    as ``+++ ``, exactly like a file header. Tracking whether the parser is
    inside a hunk resolves it, since a header never appears inside one.

    The line counter comes from the hunk header and advances on added and
    context lines but not on removed ones, which is how a line number in the new
    file is reached. Context lines are counted even though ``--unified=0`` emits
    none, so that a fixture diff generated with default context parses correctly
    too.
    """
    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

    entries: list[tuple[str, int, str]] = []
    path: str | None = None
    line_number = 0
    in_hunk = False

    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            path, in_hunk = None, False
            continue
        if not in_hunk and raw.startswith("+++ "):
            target = raw[4:].strip()
            # /dev/null is a deleted file: its hunks hold only removed lines.
            path = None if target == "/dev/null" else _strip_diff_prefix(target)
            continue
        if not in_hunk and raw.startswith("--- "):
            continue
        match = hunk_re.match(raw)
        if match:
            line_number = int(match.group(1))
            in_hunk = True
            continue
        if not in_hunk:
            # index / mode / similarity / rename / binary header lines.
            continue
        if raw.startswith("+"):
            if path is not None:
                entries.append((path, line_number, raw[1:]))
            line_number += 1
        elif raw.startswith("-"):
            pass  # removed line: not part of what the pull request adds
        elif raw.startswith("\\"):
            pass  # "\ No newline at end of file"
        else:
            line_number += 1  # context line

    return entries


def _strip_diff_prefix(target: str) -> str:
    """Drop the ``b/`` that ``git diff`` puts in front of the new file's path."""
    return target[2:] if target.startswith(("a/", "b/")) else target


def load_allowlist(path: Path) -> tuple[set[str], set[str], list[str]]:
    """Read the allowlist, returning (account IDs, instance IDs, problems).

    The shape is two required objects, each mapping an identifier to the reason
    it is safe to publish::

        {
          "account_ids":  {"123456789012": "AWS documentation example"},
          "instance_ids": {"i-0123456789abcdef0": "AWS documentation example"}
        }

    The reason is mandatory. An allowlist entry waives a check on published
    content, and the reason is what a reviewer judges the waiver on.

    **The parser fails closed**: an unreadable file, invalid JSON, a top level
    that is not an object, a missing key, a value that is not an object, a key
    that does not look like the identifier it claims to be, or a reason that is
    missing or blank — each is reported as a problem, and when there is any
    problem the function grants no allowance at all.

    That is the opposite of how ``parse_label_names`` in validate_skill_evals.py
    treats a mangled value, and deliberately so. The label that script reads can
    only make its check stricter, so treating a mangled one as absent is safe.
    An allowlist only ever makes this check *weaker*. Degrading a broken
    allowlist to "allow nothing" would be noisy but safe, and degrading it to
    "allow everything" would silently waive a real leak — so rather than guess,
    a broken allowlist is itself an error, reported with the reason and exiting
    with the configuration failure code.
    """
    rel = path.name
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return set(), set(), [f"cannot read allowlist `{path}`: {exc}"]
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return set(), set(), [f"`{rel}` is not valid JSON ({exc})"]

    if not isinstance(raw, dict):
        return (
            set(),
            set(),
            [f"`{rel}` must be a JSON object with `account_ids` and `instance_ids`"],
        )

    problems: list[str] = []
    collected: dict[str, set[str]] = {"account_ids": set(), "instance_ids": set()}
    patterns = {"account_ids": ACCOUNT_RE, "instance_ids": INSTANCE_RE}

    for key, pattern in patterns.items():
        if key not in raw:
            problems.append(f"`{rel}` is missing the `{key}` object")
            continue
        body = raw[key]
        if not isinstance(body, dict):
            problems.append(
                f"`{rel}` key `{key}` must be an object mapping identifier to reason"
            )
            continue
        for identifier, reason in body.items():
            if not isinstance(identifier, str) or not pattern.fullmatch(identifier):
                problems.append(
                    f"`{rel}` entry `{identifier}` under `{key}` is not a valid "
                    "identifier of that type"
                )
                continue
            if not isinstance(reason, str) or not reason.strip():
                problems.append(
                    f"`{rel}` entry `{identifier}` needs a non-empty reason "
                    "explaining why the value is safe to publish"
                )
                continue
            collected[key].add(_normalize(identifier))

    if problems:
        # Fail closed: no allowance at all while the file is broken.
        return set(), set(), problems

    return collected["account_ids"], collected["instance_ids"], []


def _normalize(value: str) -> str:
    """Lowercase an identifier and drop any ``_<digits>`` redaction suffix.

    Comparisons against the allowlist use this form, so ``i-1234567890abcdef0``,
    ``I-1234567890ABCDEF0`` and ``i-1234567890abcdef0_2`` all resolve to the one
    allowlist entry.
    """
    return re.sub(r"_[0-9]+$", "", value).lower()


def _inside_json_string(line: str, pos: int) -> bool:
    """True when ``pos`` in ``line`` falls inside a double-quoted JSON string.

    Counts the double quotes before ``pos`` that are not escaped — a quote
    preceded by an odd run of backslashes is escaped — and returns whether that
    count is odd.

    Used only for account IDs in ``.json``/``.jsonl`` files: AWS always writes
    an account ID as a JSON string, so a twelve-digit run appearing as a bare
    JSON number is a byte count or a similar measurement rather than an account
    ID. Instance IDs are never bare numbers, so the exclusion never applies to
    them.

    The limits, stated plainly: this reads one line in isolation and is not a
    JSON parse. A fragment with unbalanced quotes, a value split across lines,
    or a string containing an unescaped-looking quote can be judged wrongly, and
    the file type is taken from the extension rather than from the content. It
    is a heuristic that trades a small chance of missing an identifier for not
    failing every pull request that adds a twelve-digit byte count to an eval
    result.
    """
    quotes = 0
    index = 0
    while index < pos:
        if line[index] == "\\":
            index += 2  # skip the escaped character, whatever it is
            continue
        if line[index] == '"':
            quotes += 1
        index += 1
    return quotes % 2 == 1


def _excluded_paths(repo_root: Path, allowlist: Path) -> set[str]:
    """Repository-relative paths whose added lines are never scanned.

    The allowlist file itself, so that adding ``753240598075`` to it does not
    make the scanner report that very entry. Both the file in use and the
    default are listed, so a local run against a temporary allowlist still
    skips the committed one, which is the file a pull request would be editing.
    """
    excluded = {DEFAULT_ALLOWLIST}
    try:
        excluded.add(allowlist.resolve().relative_to(repo_root).as_posix())
    except ValueError:
        pass  # a --allowlist outside the repository has no diff path to skip
    return excluded


def scan_added_lines(
    entries: list[tuple[str, int, str]],
    accounts: set[str],
    instances: set[str],
    excluded: set[str],
) -> tuple[list[Finding], list[str]]:
    """Find identifiers in added lines, returning (findings, warnings).

    The marker is applied *after* the patterns rather than before. A line that
    mentions ``aws-id-ok`` but carries no identifier is suppressing nothing, so
    it neither counts as an opt-out nor earns the missing-reason warning —
    without which every line of documentation or source that names the marker,
    this file's own ``MARKER`` constant included, would be warned about.
    """
    findings: list[Finding] = []
    warnings: list[str] = []

    for path, line_number, text in entries:
        if path in excluded:
            continue

        suffix = Path(path).suffix.lower()
        is_json = suffix in JSON_SUFFIXES

        candidates: list[Finding] = []

        for match in ACCOUNT_RE.finditer(text):
            if _normalize(match.group(1)) in accounts:
                continue
            if is_json and not _inside_json_string(text, match.start()):
                continue  # a bare JSON number, so not an account ID
            candidates.append(
                Finding(path, line_number, ACCOUNT_KIND, match.group(0))
            )

        for match in INSTANCE_RE.finditer(text):
            if _normalize(match.group(1)) in instances:
                continue
            candidates.append(
                Finding(path, line_number, INSTANCE_KIND, match.group(0))
            )

        if not candidates:
            continue

        if suffix in COMMENT_BEARING_SUFFIXES and MARKER in text:
            if not MARKER_WITH_REASON_RE.search(text):
                warnings.append(
                    f"{path}:{line_number} suppresses the AWS identifier scan with "
                    f"a bare `{MARKER}` marker; write `{MARKER}: <reason>` so a "
                    "reviewer can see why the value is safe to publish"
                )
            continue

        findings += candidates

    return findings, warnings


def _annotate(findings: list[Finding], warnings: list[str]) -> None:
    """Emit GitHub Actions annotations, one per finding.

    Each finding is anchored with ``file=`` and ``line=`` so it appears inline on
    the pull request's diff, next to the line that added the identifier.
    Annotations are single-line, so any embedded newline is flattened.
    """
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    for finding in findings[:MAX_PRINTED_FINDINGS]:
        message = (
            f"{finding.kind} `{finding.value}` was added on this line. {REMEDIATION}"
        )
        print(
            f"::error file={finding.path},line={finding.line},"
            f"title=AWS identifier::{message.replace(chr(10), ' ')}"
        )
    for warning in warnings:
        print(f"::warning title=AWS identifier::{warning.replace(chr(10), ' ')}")


def _write_summary(
    findings: list[Finding], warnings: list[str], problems: list[str]
) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    lines = ["## AWS identifier scan", ""]

    if problems:
        lines.append("### Allowlist problems")
        lines.append("")
        lines += [f"- {problem}" for problem in problems]
        lines += [
            "",
            f"No allowance was granted while `{DEFAULT_ALLOWLIST}` is broken, so "
            "findings below may include values the allowlist is meant to cover. "
            "Fix the file and re-run.",
            "",
        ]

    if not findings:
        lines.append(
            "No AWS account IDs or EC2 instance IDs were added by this pull request."
        )
    else:
        lines.append(f"{len(findings)} finding(s) on lines this pull request adds:")
        lines.append("")
        lines.append("| File | Line | Kind | Value |")
        lines.append("| --- | --- | --- | --- |")
        for finding in findings[:MAX_PRINTED_FINDINGS]:
            lines.append(
                f"| `{finding.path}` | {finding.line} | {finding.kind} "
                f"| `{finding.value}` |"
            )
        remainder = len(findings) - MAX_PRINTED_FINDINGS
        if remainder > 0:
            lines.append(f"| … and {remainder} more | | | |")
        lines += ["", REMEDIATION]

    if warnings:
        lines += ["", "### Warnings", ""]
        lines += [f"- {warning}" for warning in warnings]

    with open(summary_path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


# Boundary cases for the account-ID pattern, checked against the pattern itself
# rather than through the allowlist, so each one states a property of the
# matching and nothing else. Every entry is a string to match and the list of
# account IDs the pattern is expected to pull out of it.
#
# These are here because both guards in ACCOUNT_RE suppress matches, and a
# suppressing guard that grows too broad fails silently: identifiers stop being
# reported, and a passing check is the only symptom.
#
# Every case that is expected to match uses an allowlisted documentation
# placeholder rather than an invented twelve-digit value. The cases exercise
# ACCOUNT_RE directly, and the allowlist is applied later, in
# ``scan_added_lines`` — so a placeholder proves the pattern matches just as
# well as a real-looking number would, and this file does not become the one
# place in the repository carrying identifiers that the check itself would
# report. Allowlist suppression is covered by the second group of cases in
# ``self_check``.
ACCOUNT_PATTERN_CASES: tuple[tuple[str, list[str]], ...] = (
    # The UUID guard. Nothing to report: the twelve digits are a UUID's final
    # group.
    ("a618bd73-f5dc-4e6b-b1f4-123412341234", []),
    # Upper-case hex is a UUID too.
    ("A618BD73-F5DC-4E6B-B1F4-123412341234", []),
    # The guard must not reach further than a real 8-4-4-4 hex prefix. Here the
    # first group is not hex, so this is not a UUID and the digits are reported.
    ("zzzzzzzz-f5dc-4e6b-b1f4-123456789012", ["123456789012"]),
    # A hyphen before the digits is not a UUID on its own.
    ("stack-1234-111122223333", ["111122223333"]),
    # The alphanumeric guard. A twelve-digit run inside a longer hex token
    # belongs to the token, not to an account.
    ("eni-097816109986f5e1d", []),
    ("vol-0a1b2c3d123456789012", []),
    # Still reported in the places an account ID actually appears.
    ("arn:aws:iam::123456789012:role/Example", ["123456789012"]),
    ('"accountId": "123456789012",', ["123456789012"]),
    ("123456789012", ["123456789012"]),
    # A thirteen-digit millisecond timestamp has no twelve-digit window to find.
    ("1727000000000", []),
    # The redaction suffix is consumed, and the identifier reported without it.
    ("012345678901_7", ["012345678901"]),
)


def self_check(accounts: set[str], instances: set[str]) -> list[str]:
    """Confirm the pattern boundaries hold and every allowlist entry is suppressed.

    Two groups of cases, both covering failures that are silent — they stop
    identifiers from being *reported* rather than start reporting things
    falsely, so a passing check is the only symptom and no pull request reveals
    them.

    The first group walks ACCOUNT_PATTERN_CASES, which pins the two guards in
    ACCOUNT_RE: a UUID's final group is not an account ID, and neither is a
    twelve-digit run inside a longer hex token, while an account ID in an ARN,
    in JSON, or after a plain hyphen still is.

    The second group walks every entry in the allowlist in use, builds a
    synthetic added line for the bare form and for a ``_7``-suffixed form, and
    reports any that produced a finding. The redaction suffix is the part of the
    matching that is easiest to get wrong: a ``\\b``-anchored pattern does not
    match ``i-1234567890abcdef0_3`` at all.

    Run it with ``--self-check``. It reads nothing but the allowlist and runs no
    ``git`` command.
    """
    failures: list[str] = []

    for text, expected in ACCOUNT_PATTERN_CASES:
        found = [match.group(1) for match in ACCOUNT_RE.finditer(text)]
        if found != expected:
            failures.append(
                f"account pattern on `{text}` produced {found or 'no match'}, "
                f"expected {expected or 'no match'}"
            )

    for identifier in sorted(accounts | instances):
        for value in (identifier, f"{identifier}_7"):
            entries = [("docs/self-check.md", 1, f"value {value} on a line")]
            findings, _ = scan_added_lines(entries, accounts, instances, set())
            if findings:
                failures.append(
                    f"allowlisted `{identifier}` was still reported as "
                    f"`{findings[0].value}` when written as `{value}`"
                )
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-ref",
        default="origin/main",
        help="Base ref of the pull request (default: origin/main).",
    )
    parser.add_argument(
        "--head-ref",
        default="HEAD",
        help="Head ref of the pull request (default: HEAD).",
    )
    parser.add_argument(
        "--allowlist",
        default=DEFAULT_ALLOWLIST,
        metavar="PATH",
        help=(
            "Allowlist of identifiers that are safe to publish, each with a "
            f"reason (default: {DEFAULT_ALLOWLIST}). A relative path is resolved "
            "against the repository root."
        ),
    )
    parser.add_argument(
        "--diff-file",
        default=None,
        metavar="PATH",
        help=(
            "Scan a saved unified diff instead of running git; `-` reads stdin. "
            "Intended for testing the scanner against a fixture."
        ),
    )
    parser.add_argument(
        "--self-check",
        action="store_true",
        help=(
            "Scan nothing; instead confirm every allowlist entry is suppressed "
            "both bare and with a `_N` redaction suffix, and exit non-zero if "
            "any is not."
        ),
    )
    args = parser.parse_args(argv)

    repo_root = _repo_root()

    allowlist = Path(args.allowlist)
    if not allowlist.is_absolute():
        allowlist = repo_root / allowlist

    accounts, instances, problems = load_allowlist(allowlist)
    for problem in problems:
        _error(problem)

    if args.self_check:
        if problems:
            return 2
        failures = self_check(accounts, instances)
        for failure in failures:
            _error(failure)
        total = len(accounts) + len(instances)
        print(
            f"Self-check: {len(ACCOUNT_PATTERN_CASES)} account pattern "
            f"boundary case(s), {total} allowlist entr(ies) bare and "
            f"`_N`-suffixed, {len(failures)} failure(s)."
        )
        return 1 if failures else 0

    if args.diff_file:
        diff_text = read_diff_file(args.diff_file)
    else:
        diff_text = read_diff(repo_root, args.base_ref, args.head_ref)
    if diff_text is None:
        return 2

    entries = added_lines(diff_text)
    if not entries:
        print("This pull request adds no lines; nothing to scan.")
        _write_summary([], [], problems)
        return 2 if problems else 0

    findings, warnings = scan_added_lines(
        entries, accounts, instances, _excluded_paths(repo_root, allowlist)
    )

    for finding in findings[:MAX_PRINTED_FINDINGS]:
        print(
            f"FOUND  {finding.path}:{finding.line}  {finding.kind}  {finding.value}"
        )
    remainder = len(findings) - MAX_PRINTED_FINDINGS
    if remainder > 0:
        print(f"       … and {remainder} more finding(s) not printed.")

    for warning in warnings:
        _warn(warning)

    _annotate(findings, warnings)
    _write_summary(findings, warnings, problems)

    print(
        f"\n{len(entries)} added line(s) scanned: {len(findings)} finding(s).",
        file=sys.stderr,
    )

    if problems:
        print(
            f"\nThe allowlist `{DEFAULT_ALLOWLIST}` is broken, so no identifier was "
            "allowed. Fix the problems reported above and re-run.",
            file=sys.stderr,
        )
        return 2
    if findings:
        print(f"\n{REMEDIATION}", file=sys.stderr)
        return 1

    print("No AWS account IDs or EC2 instance IDs were added by this pull request.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
