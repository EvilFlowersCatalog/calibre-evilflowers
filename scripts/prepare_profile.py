"""Point the isolated dev profile at the dev library and the dev catalog. Run under calibre-debug.

Only touches the config directory named by CALIBRE_CONFIG_DIRECTORY, and refuses
to run without it, so it cannot modify your normal Calibre profile. The API token
is not written here; enter it in the plugin's settings inside the dev Calibre.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    config_dir = os.environ.get("CALIBRE_CONFIG_DIRECTORY")
    if not config_dir or ".dev" not in Path(config_dir).parts:
        print("refusing: CALIBRE_CONFIG_DIRECTORY must point into the repository's .dev/ directory", file=sys.stderr)
        return 2

    from calibre.constants import config_dir as active_config_dir
    from calibre.gui2 import gprefs
    from calibre.utils.config import JSONConfig, prefs

    assert Path(active_config_dir).resolve() == Path(config_dir).resolve(), active_config_dir

    library = str(Path(sys.argv[-1]).resolve())
    prefs["library_path"] = library
    gprefs["welcome_wizard_was_run"] = True  # also skips the wizard's own library picker

    plugin = JSONConfig("plugins/evilflowers")
    plugin["base_url"] = os.environ.get("EVILFLOWERS_DEV_URL", plugin.get("base_url", ""))
    plugin["catalog"] = os.environ.get("EVILFLOWERS_DEV_CATALOG", plugin.get("catalog", ""))
    print(f"dev profile: {active_config_dir} -> library {library}, catalog {plugin['catalog']} @ {plugin['base_url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
