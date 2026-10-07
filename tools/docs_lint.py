#!/usr/bin/env python3
"""Verify the project docs still describe the code.

The docs are only useful if their claims are true. This checks the claims that
can be checked mechanically: referenced paths exist, the script inventory is
complete in both directions, the map stays small, and nothing states a count
that will rot the next time the code changes.

Run from anywhere:  python tools/docs_lint.py
Exit code 0 = clean, 1 = at least one failure.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Size budgets follow from how each file is read. The map is auto-loaded into
# every session, so it is capped hard. Detail docs are read whole when opened,
# so they are capped at the point where an agent starts skimming instead of
# reading. An index is never read whole and needs no cap.
MAP_FILE = "AGENTS.md"
MAP_MAX_LINES = 80
DOC_MAX_LINES = 400

# Docs whose claims are checked. Everything else in the repo is code.
DOC_PATTERNS = ["AGENTS.md", "CLAUDE.md", "README.md", "docs/*.md", "docs/*/*.md"]

# This project's modules sit at the repo root rather than in a scripts/
# directory. Tests and the doc tooling itself are not part of the inventory.
CODE_DIR = "."
INVENTORY_DOCS = ["docs/architecture.md", "ARCHITECTURE.md"]
EXCLUDE_MODULES = {"setup.py", "conftest.py"}

# Decision records are dated history. "Failed 6 of 6 JDs in Phase 9" stays true
# forever, and a record of a script that was never built necessarily names a
# file that does not exist. History docs are exempt from both the count check
# and the path check; everything describing the current system is not.
# writing-docs.md quotes counts as examples of what not to write, so it is
# exempt for the same reason: it reports rather than asserts.
HISTORY_DOCS = {"decisions.md", "writing-docs.md"}

COUNTABLE = r"scripts?|companies|jobs?|JDs?|resumes?|files?|entries|tests?|memories"
COUNT_ASSERTION = re.compile(rf"\b\d+\+?\s+(?:real\s+|verified\s+)?(?:{COUNTABLE})\b", re.I)

ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")

# Paths are only checked when written in backticks — an explicit claim that
# something exists at that location, rather than prose that happens to contain
# a slash.
BACKTICKED = re.compile(r"`([^`\n]+)`")

# Placeholders and globs are templates, not claims about the filesystem.
NOT_A_REAL_PATH = re.compile(r"[<>*{}$|]|\.\.\.")
PATH_SUFFIXES = {".py", ".md", ".json", ".jsonl", ".db", ".txt", ".toml", ".lock", ".docx", ".pdf"}


class Findings:
    """Collects failures so every check runs before the script exits."""

    def __init__(self) -> None:
        self.items: list[tuple[str, str]] = []

    def add(self, where: str, problem: str) -> None:
        self.items.append((where, problem))

    def __len__(self) -> int:
        return len(self.items)


def doc_files() -> list[Path]:
    found: list[Path] = []
    for pattern in DOC_PATTERNS:
        found.extend(sorted(REPO.glob(pattern)))
    return [p for p in found if p.is_file()]


def rel(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


GENERATED_PATHS = {'logs/metrics.jsonl', 'logs/job_monitor.log',
                   'reports/history.html', 'reports/jobs_latest.html',
                   'config.json', 'history.json', 'job_state.json', 'jobs.db'}


def looks_like_path(text: str) -> bool:
    """True for text that claims something exists at a repo-relative location.

    Absolute paths are skipped: they point outside the repo, so this checker
    has no business asserting anything about them.
    """
    if text in GENERATED_PATHS:
        return False
    if NOT_A_REAL_PATH.search(text) or text.startswith(("-", "--")):
        return False
    # Anything with a URI scheme is an address, not a path on this disk. That
    # covers http(s) and the custom workday:// netflix:// schemes config uses.
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", text):
        return False
    if " " in text.strip():
        return False
    if re.match(r"^([a-zA-Z]:|[/\\~])", text):
        return False
    if "/" in text:
        return True
    # A bare filename is only a claim about the repo when it names code.
    # Bare data filenames — verdict.json, metrics.jsonl — name per-run outputs
    # written under out/<job>/, which exist after a run and not before.
    return Path(text).suffix == ".py"


# Directories that hold generated output or dependencies. Walking them is the
# difference between a check that runs in a blink and one nobody waits for.
SKIP_DIRS = {".git", "out", "jobs", "__pycache__", ".venv", "node_modules", ".pytest_cache"}

_filename_index: set[str] | None = None


def tracked_filenames() -> set[str]:
    """Every filename in the repo, indexed once.

    Built lazily and cached: the alternative is one full-tree walk per
    unresolved reference, which on a repo with generated output turns a
    sub-second check into a ten-second one.
    """
    global _filename_index
    if _filename_index is not None:
        return _filename_index

    names: set[str] = set()
    stack = [REPO]
    while stack:
        current = stack.pop()
        try:
            for entry in current.iterdir():
                if entry.is_dir():
                    if entry.name not in SKIP_DIRS:
                        stack.append(entry)
                else:
                    names.add(entry.name)
        except OSError:
            continue

    _filename_index = names
    return names


def exists_anywhere(filename: str) -> bool:
    """Whether a bare filename appears somewhere in the tree."""
    if "/" in filename or "\\" in filename:
        return False
    return filename in tracked_filenames()


def check_referenced_paths(docs: list[Path], out: Findings) -> None:
    """Every backticked path in a doc must exist on disk."""
    for doc in docs:
        if doc.name in HISTORY_DOCS:
            continue
        for line_no, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            for candidate in BACKTICKED.findall(line):
                target = candidate.strip().rstrip("/")
                if not looks_like_path(target):
                    continue
                if (REPO / target).exists():
                    continue
                # A bare filename may live anywhere in the tree.
                if exists_anywhere(target):
                    continue
                out.add(f"{rel(doc)}:{line_no}", f"path does not exist: {target}")


def check_script_inventory(docs: list[Path], out: Findings) -> None:
    """Scripts and their documentation must agree in both directions."""
    code_dir = REPO / CODE_DIR
    if not code_dir.is_dir():
        return

    on_disk = {p.name for p in code_dir.glob("*.py")
               if not p.name.startswith(("_", "test_")) and p.name not in EXCLUDE_MODULES}

    inventories = [REPO / name for name in INVENTORY_DOCS if (REPO / name).is_file()]
    if not inventories:
        out.add(CODE_DIR, f"no inventory doc found (expected one of: {', '.join(INVENTORY_DOCS)})")
        return

    documented_anywhere: set[str] = set()
    for doc in docs:
        text = doc.read_text(encoding="utf-8")
        documented_anywhere |= {name for name in on_disk if name in text}

    inventory_text = "\n".join(p.read_text(encoding="utf-8") for p in inventories)
    inventoried = {name for name in on_disk if name in inventory_text}

    for name in sorted(on_disk - inventoried):
        out.add(rel(inventories[0]), f"module exists but is not documented: {name}")

    mentioned = set(re.findall(r"\b([a-z_][a-z0-9_]*\.py)\b", inventory_text))
    for name in sorted(mentioned - on_disk):
        if (REPO / name).exists() or exists_anywhere(name):
            continue
        out.add(rel(inventories[0]), f"documented module does not exist: {name}")


def check_map_size(out: Findings) -> None:
    """The map is loaded every session, so it stays within its budget."""
    map_path = REPO / MAP_FILE
    if not map_path.is_file():
        out.add(MAP_FILE, "missing — the map is what loads at session start")
        return

    lines = len(map_path.read_text(encoding="utf-8").splitlines())
    if lines > MAP_MAX_LINES:
        out.add(MAP_FILE, f"{lines} lines, budget is {MAP_MAX_LINES} — move detail into docs/")


def check_count_assertions(docs: list[Path], out: Findings) -> None:
    """Counts rot. Docs point at the source instead of copying the number."""
    for doc in docs:
        if doc.name in HISTORY_DOCS:
            continue
        for line_no, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            match = COUNT_ASSERTION.search(line)
            if match:
                out.add(f"{rel(doc)}:{line_no}", f"counts something: \"{match.group(0)}\" — point at the source instead")


def check_docs_are_routed(docs: list[Path], out: Findings) -> None:
    """A doc the map never names is a doc no agent will find.

    This is the guard against context being missed: retrieval works by looking
    the question up in the map, not by scoring keywords, so every detail doc
    has to be reachable from there.
    """
    map_path = REPO / MAP_FILE
    if not map_path.is_file():
        return

    routing = map_path.read_text(encoding="utf-8")
    for doc in docs:
        name = rel(doc)
        if not name.startswith("docs/"):
            continue
        if name in routing or doc.name in routing:
            continue
        out.add(name, "not named in the map — nothing routes here, so it will never be read")


def check_doc_size(docs: list[Path], out: Findings) -> None:
    """Detail docs are read whole, so they stay small enough to be read whole."""
    for doc in docs:
        if doc.name == MAP_FILE:
            continue
        lines = len(doc.read_text(encoding="utf-8").splitlines())
        if lines > DOC_MAX_LINES:
            out.add(rel(doc), f"{lines} lines, budget is {DOC_MAX_LINES} — split by question, not by size")


def check_single_source(docs: list[Path], out: Findings) -> None:
    """A script described in two places is a conflict waiting to happen.

    Conflicting entries arise when the same fact is written down twice and only
    one copy gets updated. The fix is structural: every script is described in
    exactly one doc, and everything else links to it.
    """
    code_dir = REPO / CODE_DIR
    if not code_dir.is_dir():
        return

    describing_docs: dict[str, list[str]] = {}
    for script in code_dir.glob("*.py"):
        if script.name.startswith(("_", "test_")) or script.name in EXCLUDE_MODULES:
            continue
        # A doc describes a script if it gives it its own heading or table row,
        # rather than merely mentioning it in a sentence.
        pattern = re.compile(rf"^\s*(#+\s*|\|\s*)`?{re.escape(script.name)}`?", re.M)
        homes = [rel(d) for d in docs if pattern.search(d.read_text(encoding="utf-8"))]
        if len(homes) > 1:
            describing_docs[script.name] = homes

    for name, homes in sorted(describing_docs.items()):
        out.add(name, f"described in {len(homes)} docs ({', '.join(homes)}) — keep one, link the rest")


def check_doc_freshness(out: Findings) -> None:
    """Code newer than the doc that describes it is unreviewed drift."""
    code_dir = REPO / CODE_DIR
    inventories = [REPO / name for name in INVENTORY_DOCS if (REPO / name).is_file()]
    if not code_dir.is_dir() or not inventories:
        return

    scripts = [p for p in code_dir.glob("*.py")
               if not p.name.startswith(("_", "test_")) and p.name not in EXCLUDE_MODULES]
    if not scripts:
        return

    newest = max(scripts, key=lambda p: p.stat().st_mtime)
    doc = inventories[0]
    if newest.stat().st_mtime > doc.stat().st_mtime:
        out.add(rel(doc), f"older than {newest.name} — code changed after the doc describing it")


def check_map_has_no_diary(out: Findings) -> None:
    """Dates in the map mean run logs are creeping back in."""
    map_path = REPO / MAP_FILE
    if not map_path.is_file():
        return

    for line_no, line in enumerate(map_path.read_text(encoding="utf-8").splitlines(), 1):
        if ISO_DATE.search(line):
            out.add(f"{MAP_FILE}:{line_no}", "dated entry in the map — session logs belong in docs/decisions.md")


def main() -> int:
    docs = doc_files()
    if not docs:
        print(f"docs_lint: no docs found under {REPO}")
        return 1

    out = Findings()
    check_referenced_paths(docs, out)
    check_script_inventory(docs, out)
    check_map_size(out)
    check_doc_size(docs, out)
    check_docs_are_routed(docs, out)
    check_count_assertions(docs, out)
    check_map_has_no_diary(out)

    if not out:
        print(f"docs_lint: clean — {len(docs)} docs check out against the code")
        return 0

    print(f"docs_lint: {len(out)} problem(s)\n")
    for where, problem in out.items:
        print(f"  {where}\n      {problem}")
    print("\nFix the docs, or the claim, before committing.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
