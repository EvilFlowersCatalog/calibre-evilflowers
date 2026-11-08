"""Re-vendor the stdlib layers of evilflowers-protocol-python into the plugin.

Usage: python3 scripts/vendor_protocol.py /path/to/evilflowers-protocol-python

Copies errors.py and _version.py verbatim, copies transport.py and applies
scripts/transport-no-pydantic.patch, then pins the checkout's commit in
vendor/evilflowers_protocol/__init__.py and UPSTREAM.md. A patch that no longer
applies stops the script; refresh the patch by hand in that case.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "calibre_plugin" / "vendor" / "evilflowers_protocol"
PATCH = ROOT / "scripts" / "transport-no-pydantic.patch"
VERBATIM = ("errors.py", "_version.py")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    upstream = Path(argv[1]).resolve()
    package = upstream / "evilflowers_protocol"
    if not (package / "transport.py").is_file():
        print(f"not an evilflowers-protocol-python checkout: {upstream}", file=sys.stderr)
        return 2

    commit = subprocess.run(
        ["git", "-C", str(upstream), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()

    for name in VERBATIM:
        shutil.copyfile(package / name, VENDOR / name)
    shutil.copyfile(package / "transport.py", VENDOR / "transport.py")
    subprocess.run(
        ["patch", "--forward", "--no-backup-if-mismatch", str(VENDOR / "transport.py"), str(PATCH)], check=True
    )

    for target in (VENDOR / "__init__.py", VENDOR / "UPSTREAM.md"):
        text = target.read_text()
        target.write_text(re.sub(r"[0-9a-f]{40}", commit, text))

    print(f"vendored {commit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
