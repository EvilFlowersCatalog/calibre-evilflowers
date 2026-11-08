"""Build the installable plugin ZIP: dist/evilflowers-<version>.zip.

Install it with Calibre → Preferences → Plugins → Load plugin from file, or
``calibre-customize -a dist/evilflowers-<version>.zip``.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "calibre_plugin"
SKIP_DIRS = {"__pycache__"}
SKIP_SUFFIXES = {".pyc", ".pyo"}


def version() -> str:
    match = re.search(r"version = \((\d+), (\d+), (\d+)\)", (PLUGIN / "__init__.py").read_text())
    if match is None:
        raise SystemExit("no version tuple in calibre_plugin/__init__.py")
    return ".".join(match.groups())


def main() -> int:
    target = ROOT / "dist" / f"evilflowers-{version()}.zip"
    target.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(PLUGIN.rglob("*")):
            if path.is_dir() or SKIP_DIRS & set(path.parts) or path.suffix in SKIP_SUFFIXES:
                continue
            archive.write(path, path.relative_to(PLUGIN).as_posix())
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
