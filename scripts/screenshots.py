"""Render the plugin's windows to PNG, in Calibre's light and dark palettes.

Run it through scripts/dev.sh, which uses Calibre's own Python and the isolated .dev/
profile:

    scripts/dev.sh screenshots              every window state -> .dev/screenshots/
    scripts/dev.sh screenshots --docs       the set docs/README.md shows -> docs/screenshots/
    scripts/dev.sh screenshots sync browse  only the states whose name contains a word

Everything runs offscreen against the test suite's FakeCatalog and throwaway libraries,
so no network and no real library is touched. Plugin preferences changed while
rendering are restored afterwards.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path
from types import SimpleNamespace

os.environ["QT_QPA_PLATFORM"] = "offscreen"
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO))

from calibre.ebooks.metadata.book.base import Metadata  # noqa: E402
from calibre.gui2.palette import dark_palette, light_palette  # noqa: E402
from calibre.library import db as open_library  # noqa: E402
from epub import epub  # noqa: E402
from fake_catalog import PASSWORD, TOKEN, USERNAME, FakeCatalog  # noqa: E402
from qt.core import QApplication, QIcon, QItemSelectionModel, QVBoxLayout, QWidget  # noqa: E402
from test_browse import jpeg  # noqa: E402
from test_gui import FakeGui, app, png, wait_for  # noqa: E402
from test_settings_gui import library_with_columns  # noqa: E402

from calibre_plugin.remote import auth  # noqa: E402
from calibre_plugin.settings import prefs  # noqa: E402

SIZE = (900, 650)
WIDE = (1100, 680)  # the sync window's comparison panel needs the room
EPUB = "application/epub+zip"
ACTION = SimpleNamespace(show_settings=lambda parent=None: None)

# The states docs/README.md shows; --docs renders only these, into docs/screenshots/.
DOCS = {"settings-signed-in", "signin-empty", "sync-setup", "sync-plan", "browse-results", "highlights"}

out_dir = REPO / ".dev" / "screenshots"
saved: list[Path] = []
failures: list[str] = []


def settle(rounds: int = 10) -> None:
    for _ in range(rounds):
        QApplication.processEvents()


def save(widget: QWidget, name: str, mode: str, size: tuple[int, int] | None = SIZE) -> None:
    if size:
        widget.resize(*size)
    settle()
    path = out_dir / f"{name}-{mode}.png"
    widget.grab().save(str(path))
    saved.append(path)


def host(widget: QWidget) -> QWidget:
    """Put a plain widget in a window, like Calibre's preferences dialog does."""
    window = QWidget()
    layout = QVBoxLayout(window)
    layout.addWidget(widget)
    window.show()
    return window


class Env:
    """A fresh fake catalog and temporary library for one rendered state."""

    def __init__(self) -> None:
        self.fake = FakeCatalog().start()
        self.tmp = Path(tempfile.mkdtemp(prefix="evilflowers-screenshots-"))
        prefs["base_url"], prefs["token"], prefs["catalog"] = self.fake.base_url, TOKEN, self.fake.catalog_url_name
        prefs["direction"], prefs["show_in_sync"] = "both", False
        prefs["api_key_id"], prefs["username"] = "", ""
        prefs["shelf_only"], prefs["highlights_column"] = False, ""
        self.db = open_library(str(self.tmp / "lib")).new_api

    def add_book(self, title: str, uuid: str | None = None, cover: bytes | None = None) -> int:
        path = self.tmp / f"{title}.epub"
        path.write_bytes(epub(title))
        mi = Metadata(title, ["Ada Lovelace"])
        if uuid:
            mi.uuid = uuid
        [book_id], _ = self.db.add_books([(mi, {"EPUB": str(path)})], preserve_uuid=bool(uuid), run_hooks=False)
        if cover:
            self.db.set_cover({book_id: cover})
        return book_id

    def close(self) -> None:
        self.db.close()
        self.fake.stop()
        shutil.rmtree(self.tmp, True)


def step(name: str, render, mode: str) -> None:
    kept = {key: prefs[key] for key in prefs.defaults}
    env = Env()
    try:
        render(env, mode)
    except Exception:
        failures.append(f"{name}-{mode}: {traceback.format_exc()}")
    finally:
        settle()
        env.close()
        for key, value in kept.items():
            prefs[key] = value


# ------------------------------------------------------------------ settings


def settings_signed_out(env: Env, mode: str) -> None:
    from calibre_plugin.config import ConfigWidget

    prefs["token"] = ""
    window = host(ConfigWidget(library_with_columns(**{"#highlights": "comments"})))
    save(window, "settings-signed-out", mode)
    window.close()


def settings_signed_in(env: Env, mode: str) -> None:
    from calibre_plugin.config import ConfigWidget

    key = auth.sign_in(env.fake.base_url, USERNAME, PASSWORD, key_name="screenshots", user_agent="screenshots")
    prefs["token"], prefs["api_key_id"], prefs["username"] = key.token, str(key.id), USERNAME
    env.fake.add_catalog("Another one", "another")
    widget = ConfigWidget(library_with_columns(**{"#highlights": "comments"}))
    window = host(widget)
    wait_for(lambda: widget.catalog_picker.count() == 2 and widget.refresh_button.isEnabled())
    save(window, "settings-signed-in", mode)
    window.close()


def settings_api_key(env: Env, mode: str) -> None:
    from calibre_plugin.config import ConfigWidget

    prefs["token"] = ""
    widget = ConfigWidget()
    window = host(widget)
    widget.toggle_key_area()
    widget.token.setText(TOKEN)
    widget.test_connection()
    wait_for(lambda: widget.test_button.isEnabled())
    save(window, "settings-api-key", mode)
    window.close()


def signin_empty(env: Env, mode: str) -> None:
    from calibre_plugin.login import SignInDialog

    dialog = SignInDialog(None, env.fake.base_url)
    dialog.show()
    save(dialog, "signin-empty", mode, size=None)
    dialog.reject()


def signin_error(env: Env, mode: str) -> None:
    from calibre_plugin.login import SignInDialog

    dialog = SignInDialog(None, env.fake.base_url)
    dialog.show()
    dialog.username.setText(USERNAME)
    dialog.password.setText("wrong password")
    dialog.submit()
    wait_for(lambda: dialog.task is None)
    save(dialog, "signin-error", mode, size=None)
    dialog.reject()


# ---------------------------------------------------------------------- sync


def sync_dialog(env: Env, wait: bool = True):
    from calibre_plugin.dialog import SyncDialog

    dialog = SyncDialog(FakeGui(env.db), QIcon(), ACTION)
    dialog.show()
    if wait:
        wait_for(lambda: dialog.worker is None and dialog.phase != "loading")
    return dialog


def sync_setup(env: Env, mode: str) -> None:
    prefs["token"] = ""
    dialog = sync_dialog(env, wait=False)
    save(dialog, "sync-setup", mode)
    dialog.reject()


def sync_loading(env: Env, mode: str) -> None:
    env.add_book("Local one")
    dialog = sync_dialog(env)
    dialog.phase = "loading"
    dialog._scanning = True
    dialog.update_view()
    dialog.loading_page.detail.setText("Reading the catalog… page 2 of 5")
    save(dialog, "sync-loading", mode)
    dialog.reject()


def sync_plan(env: Env, mode: str) -> None:
    """Every row state at once, with a book changed on both sides selected."""
    shared = env.add_book("The shared book with a fairly long title", cover=png("teal"))
    changed = env.add_book("Edited here")
    env.add_book("Kept in sync")
    dialog = sync_dialog(env)
    dialog.synchronize()
    wait_for(lambda: dialog.worker is None and dialog.model.results)
    entry_id = env.db.field_for("uuid", shared)
    env.fake.edit_entry(entry_id, title="The shared book (catalog edit)", publisher="Remote Press")
    env.db.set_field("title", {shared: "The shared book (Calibre edit)"})
    env.db.set_field("title", {changed: "Edited here (new title)"})
    env.add_book("Only in Calibre", cover=png("orange"))
    env.add_book("Another local book")
    env.fake.add_remote_entry("Only in the catalog", {EPUB: epub("r")}, cover=jpeg())
    env.fake.add_remote_entry("Catalog-only poetry", {EPUB: epub("p")})
    dialog.scan()
    wait_for(lambda: dialog.worker is None and dialog.phase != "loading")
    dialog.set_filter("all")
    dialog.resize(*WIDE)
    settle()
    rows = range(dialog.proxy.rowCount())
    dialog.table.selectRow(next(i for i in rows if "shared" in str(dialog.proxy.index(i, 1).data()).lower()))
    with contextlib.suppress(AssertionError):  # publisher and date arrive from a background request
        wait_for(lambda: any(diff.label == "Publisher" for diff in dialog.details.diffs), 5000)
    save(dialog, "sync-plan-conflict", mode, size=WIDE)
    dialog.reject()


def sync_in_sync(env: Env, mode: str) -> None:
    env.add_book("Local one")
    env.add_book("Local two")
    dialog = sync_dialog(env)
    dialog.synchronize()
    wait_for(lambda: dialog.worker is None and dialog.model.results)
    dialog.scan()
    wait_for(lambda: dialog.worker is None and dialog.phase != "loading")
    dialog.set_filter("pending")
    save(dialog, "sync-in-sync", mode)
    dialog.reject()


def sync_error(env: Env, mode: str) -> None:
    prefs["token"] = "wrong"
    dialog = sync_dialog(env)
    save(dialog, "sync-error", mode)
    dialog.reject()


# -------------------------------------------------------------------- browse


def browse_dialog(env: Env):
    from calibre_plugin.browse import BrowseDialog

    dialog = BrowseDialog(FakeGui(env.db), QIcon(), ACTION)
    dialog.show()
    wait_for(lambda: not dialog.busy and dialog.page == 1)
    return dialog


def browse_results(env: Env, mode: str) -> None:
    env.fake.page_size = 6
    physics = {"term": "physics", "label": "Physics"}
    poetry = {"term": "poetry", "label": "Poetry"}
    alpha = env.fake.add_remote_entry(
        "Alpha physics: an introduction to everything", {EPUB: epub("a")}, categories=[physics], cover=jpeg()
    )
    beta = env.fake.add_remote_entry("Beta physics", {EPUB: epub("b")}, categories=[physics])
    env.fake.add_remote_entry("Gamma verses", {EPUB: epub("g")}, categories=[poetry], language_code="sk")
    env.fake.add_remote_entry("Delta", {EPUB: epub("d")}, categories=[poetry, physics], cover=jpeg())
    for title in ("Epsilon", "Zeta", "Eta"):
        env.fake.add_remote_entry(title, {EPUB: epub(title)})
    env.fake.add_shelf_record(alpha)
    env.fake.add_shelf_record(beta)
    env.add_book("Beta physics", uuid=beta)
    dialog = browse_dialog(env)
    with contextlib.suppress(AssertionError):  # covers load in the background
        wait_for(lambda: alpha in dialog.model.covers, 5000)
    flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
    dialog.table.selectionModel().select(dialog.model.index(2, 0), flags)
    save(dialog, "browse-results", mode)
    dialog.reject()


def browse_empty(env: Env, mode: str) -> None:
    env.fake.add_remote_entry("Alpha", {EPUB: epub("a")})
    dialog = browse_dialog(env)
    generation = dialog._generation
    dialog.search.setText("nothing like this")
    dialog.search.returnPressed.emit()
    wait_for(lambda: dialog._generation > generation and not dialog.busy)
    save(dialog, "browse-empty", mode)
    dialog.reject()


# ---------------------------------------------------------------- highlights


def library_with_highlights_column(env: Env) -> None:
    env.db.close()
    setup = open_library(str(env.tmp / "lib2")).new_api
    setup.create_custom_column("highlights", "Highlights", "comments", False)
    setup.close()
    env.db = open_library(str(env.tmp / "lib2")).new_api


def highlights(env: Env, mode: str, empty: bool) -> None:
    from calibre_plugin.highlights_dialog import HighlightsDialog
    from calibre_plugin.settings import Settings

    library_with_highlights_column(env)
    marked = env.fake.add_remote_entry("Marked book", {EPUB: epub("m")})
    other = env.fake.add_remote_entry("Second book", {EPUB: epub("s")})
    plain = env.fake.add_remote_entry("Plain", {EPUB: epub("p")})
    long_passage = "The first passage someone highlighted, long enough to wrap onto a second line in the window."
    env.fake.add_highlight(marked, 3, long_passage)
    env.fake.add_highlight(marked, 12, "A later <passage>")
    env.fake.add_highlight(marked, 40, "Chapter note", title="Chapter notes")
    env.fake.add_highlight(other, 7, "Another book's highlight")
    ids = [env.add_book("Marked book", marked), env.add_book("Second book", other), env.add_book("Only here")]
    plain_id = env.add_book("Plain", plain)
    settings = Settings(env.fake.base_url, TOKEN, env.fake.catalog_url_name, highlights_column="#highlights")
    dialog = HighlightsDialog(FakeGui(env.db), [plain_id] if empty else ids, settings=settings)
    dialog.show()
    wait_for(lambda: dialog.worker is None and bool(dialog.books))
    save(dialog, "highlights-empty" if empty else "highlights", mode)
    dialog.reject()


STEPS = [
    ("settings-signed-out", settings_signed_out),
    ("settings-signed-in", settings_signed_in),
    ("settings-api-key", settings_api_key),
    ("signin-empty", signin_empty),
    ("signin-error", signin_error),
    ("sync-setup", sync_setup),
    ("sync-loading", sync_loading),
    ("sync-plan", sync_plan),
    ("sync-in-sync", sync_in_sync),
    ("sync-error", sync_error),
    ("browse-results", browse_results),
    ("browse-empty", browse_empty),
    ("highlights", lambda env, mode: highlights(env, mode, False)),
    ("highlights-empty", lambda env, mode: highlights(env, mode, True)),
]


def main(argv: list[str]) -> int:
    global out_dir
    args = [arg for arg in argv if arg != "--"]
    docs = "--docs" in args
    words = [arg for arg in args if not arg.startswith("--")]
    if docs:
        out_dir = REPO / "docs" / "screenshots"
    out_dir.mkdir(parents=True, exist_ok=True)

    application = app()
    for mode, palette in (("light", light_palette()), ("dark", dark_palette())):
        manager = getattr(application, "palette_manager", None)
        if manager is not None:
            (manager.set_dark_mode_palette if mode == "dark" else manager.set_light_mode_palette)()
        else:
            application.setPalette(palette)
        settle()
        for name, render in STEPS:
            if docs and name not in DOCS:
                continue
            if words and not any(word in name for word in words):
                continue
            step(name, render, mode)

    print(f"{len(saved)} screenshots in {out_dir}")
    for path in saved:
        print(f"  {path.name}")
    if failures:
        print("FAILED:\n" + "\n".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
