"""Release checks for CI: one version everywhere, and release notes from CHANGELOG.md.

    python3 scripts/release.py check [vX.Y.Z]   versions agree (and match the tag, if given)
    python3 scripts/release.py notes [X.Y.Z]    print that version's CHANGELOG section

The version lives in three places that must agree: the ``version`` tuple in
``calibre_plugin/__init__.py`` (the build and Calibre read it), ``pyproject.toml``,
and a ``## X.Y.Z - YYYY-MM-DD`` heading in ``CHANGELOG.md``. An ``## Unreleased``
section is allowed above it and is never published.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from build import version as plugin_version  # noqa: E402

HEADING = re.compile(r"^## (?P<version>\d+\.\d+\.\d+) - \d{4}-\d{2}-\d{2}\s*$")


def pyproject_version() -> str:
    match = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.MULTILINE)
    if match is None:
        raise SystemExit("no version in pyproject.toml")
    return match.group(1)


def changelog_section(version: str) -> str | None:
    """The body of ``## <version> - <date>``, without the heading; None if there is none."""
    lines = (ROOT / "CHANGELOG.md").read_text().splitlines()
    body: list[str] | None = None
    for line in lines:
        if line.startswith("## "):
            if body is not None:
                break
            match = HEADING.match(line)
            if match and match.group("version") == version:
                body = []
            continue
        if body is not None:
            body.append(line)
    return None if body is None else "\n".join(body).strip() + "\n"


def check(tag: str | None) -> int:
    plugin = plugin_version()
    problems = []
    if pyproject_version() != plugin:
        problems.append(f"pyproject.toml says {pyproject_version()}, calibre_plugin/__init__.py says {plugin}")
    if tag is not None and tag.removeprefix("v") != plugin:
        problems.append(f"tag {tag} does not match calibre_plugin/__init__.py version {plugin}")
    if not changelog_section(plugin):
        problems.append(f"CHANGELOG.md has no '## {plugin} - YYYY-MM-DD' section with content")
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    if not problems:
        print(f"version {plugin} is consistent")
    return 1 if problems else 0


def notes(version: str | None) -> int:
    section = changelog_section(version or plugin_version())
    if not section:
        print(f"error: CHANGELOG.md has no section for {version or plugin_version()}", file=sys.stderr)
        return 1
    sys.stdout.write(section)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) not in (1, 2) or argv[0] not in ("check", "notes"):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    arg = argv[1] if len(argv) == 2 else None
    if argv[0] == "check":
        return check(arg)
    return notes(arg.removeprefix("v") if arg else None)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
