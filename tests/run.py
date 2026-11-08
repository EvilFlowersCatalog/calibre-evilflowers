"""Run the test suite under Calibre's own Python:

    scripts/dev.sh test              # offline: planner, models, engine against the fake catalog
    scripts/dev.sh test-dev          # also the end-to-end test against the dev catalog
    scripts/dev.sh test -k pull      # only tests whose name contains "pull"

Refuses to run unless CALIBRE_CONFIG_DIRECTORY points into the repository's .dev/
directory, so no test can read or write your normal Calibre profile.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OFFLINE_MODULES = (
    "test_models",
    "test_client",
    "test_plan",
    "test_engine",
    "test_highlights",
    "test_theme",
    "test_gui",
    "test_settings_gui",
    "test_browse",
)


def main() -> int:
    from calibre.constants import config_dir

    if ".dev" not in Path(config_dir).parts:
        print(f"refusing to run with the Calibre profile {config_dir}; use scripts/dev.sh test", file=sys.stderr)
        return 2

    args = sys.argv[1:]
    with_dev = "--dev" in args
    pattern = args[args.index("-k") + 1] if "-k" in args else None

    sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
    loader = unittest.TestLoader()
    if pattern:
        loader.testNamePatterns = [f"*{pattern}*"]
    suite = unittest.TestSuite()
    for module in OFFLINE_MODULES + (("test_dev",) if with_dev else ()):
        suite.addTests(loader.loadTestsFromName(module))
    result = unittest.TextTestRunner(verbosity=2 if os.environ.get("VERBOSE") else 1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
