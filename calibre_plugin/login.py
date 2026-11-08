"""Sign in with a username and password, and a few small widgets the settings page shares.

The password is handled as little as possible: it is read from the field when the user
presses "Sign in", handed to a background thread for one request, and dropped. It is
never stored, logged or put in an error message, and the field is emptied whenever the
dialog closes. The access token the login returns lives only inside
:func:`..remote.auth.sign_in`; what comes back is the API key the plugin keeps instead.
"""

from __future__ import annotations

import contextlib
import functools
import threading
import urllib.parse
from uuid import UUID

from qt.core import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .remote import auth
from .remote.client import CatalogClient, site_address
from .remote.models import ApiKeyCreated
from .vendor.evilflowers_protocol.errors import CatalogError
from .worker import Task, describe_error, run_task, user_agent

SERVER_HELP = "The address of your EvilFlowers site, e.g. https://elvira.stuba.sk."
USERNAME_HELP = "The name you use to sign in on the EvilFlowers website."
PASSWORD_HELP = "Used once to create a key for this computer. Never saved."

SignInResult = tuple[str, ApiKeyCreated, str]  # (server address, the new key, username)


# --- background work ---------------------------------------------------------------
# Task and run_task live in worker.py, shared by every window.


def revoke_key(base_url: str, token: str, api_key_id: str) -> None:
    """Delete a key on the server, authenticating with the key itself. Blocking."""
    client = CatalogClient(base_url, token, timeout=15, retries=0, user_agent=user_agent())
    client.revoke_api_key(UUID(api_key_id))


# --- shared bits of UI -------------------------------------------------------------


def normalize_server(text: str) -> str:
    """The site address for what the user typed: https:// added when the scheme is missing,
    and a pasted API address (``…/api/v1``) trimmed back to the site."""
    text = text.strip().rstrip("/")
    if text and "://" not in text:
        text = f"https://{text}"
    return site_address(text)


def host_of(url: str) -> str:
    """The host name to show for a server address, or the address itself if it has none."""
    url = normalize_server(url)
    return urllib.parse.urlsplit(url).hostname or url


def helper_label(text: str, parent: QWidget | None = None) -> QLabel:
    """The small grey line under a field that says what it is for (see theme.HelperLabel)."""
    return theme.HelperLabel(text, parent)


def add_field(
    form: QFormLayout, label: str, field: QWidget | QLayout, help_text: str, *, buddy: QWidget | None = None
) -> QLabel:
    """A form row: label and field, then the helper line under the field; both carry it as a tooltip.

    The helper is a row of its own in the field column. Nested in a layout under the
    field, QFormLayout would not give the wrapped text its height and cut it off.
    """
    if buddy is None:  # never `buddy or ...`: an empty QComboBox is falsy
        buddy = field if isinstance(field, QWidget) else None
    # Fields and helpers take the form's whole width (macOS styles keep them at their size hint).
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    parent = form.parentWidget()
    caption = QLabel(label, parent)
    caption.setToolTip(help_text)
    if buddy is not None:
        buddy.setToolTip(help_text)
        caption.setBuddy(buddy)
    form.addRow(caption, field)
    helper = helper_label(help_text, parent)
    helper.setToolTip(help_text)
    helper.setContentsMargins(0, 0, 0, 4)  # a little more room before the next field than above
    form.addRow(QLabel("", parent), helper)
    return helper


def error_style() -> str:
    """Red text that stays readable on both light and dark themes."""
    return "color: #ff8a80;" if theme.is_dark() else "color: #c62828;"


def describe_sign_in_error(exc: BaseException) -> str:
    """A short message for the dialog. Never includes the password (auth scrubs it)."""
    if isinstance(exc, CatalogError):
        if exc.status is None:
            return f"Can't reach the server. Check the address and your connection. ({exc.message})"
        if exc.status == 404:
            return "There is no EvilFlowers site at this address. Check the server address."
        return exc.message
    if isinstance(exc, InterruptedError):
        return "Cancelled."
    return describe_error(exc)


def _sign_in(base_url: str, username: str, password: str, cancelled: threading.Event) -> ApiKeyCreated:
    """Runs on the task thread. If the user gave up meanwhile, the new key is revoked, not orphaned."""
    key = auth.sign_in(
        base_url, username, password, key_name=auth.default_key_name(), user_agent=user_agent(), timeout=30
    )
    if cancelled.is_set():
        with contextlib.suppress(Exception):  # best effort; else it stays in the user's key list
            revoke_key(base_url, key.token, str(key.id))
        raise InterruptedError
    return key


# --- the dialog --------------------------------------------------------------------


class SignInDialog(QDialog):
    """Ask for a username and password and trade them for an API key.

    After ``exec()`` returns Accepted, :attr:`outcome` is ``(base_url, key, username)``.
    Tests can call :meth:`submit` directly instead of ``exec()``.
    """

    def __init__(self, parent: QWidget | None = None, base_url: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("Sign in to EvilFlowers")
        self.setModal(True)
        self.setWindowIcon(theme.load_icon())
        self.outcome: SignInResult | None = None
        self.task: Task | None = None
        self._cancelled = threading.Event()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 12)
        self.header = theme.Header("Sign in to EvilFlowers", host_of(base_url), self)
        layout.addWidget(self.header)

        body = QWidget(self)
        form = QFormLayout(body)
        form.setContentsMargins(16, 8, 16, 0)
        self.server = QLineEdit(normalize_server(base_url), body)
        self.server.setPlaceholderText("https://elvira.stuba.sk")
        self.server.textChanged.connect(lambda text: self.header.set_subtitle(host_of(text)))
        add_field(form, "&Server:", self.server, SERVER_HELP)
        self.username = QLineEdit(body)
        self.username.setPlaceholderText("Username")
        add_field(form, "&Username:", self.username, USERNAME_HELP)
        self.password = QLineEdit(body)
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Password")
        add_field(form, "&Password:", self.password, PASSWORD_HELP)
        layout.addWidget(body)

        self.busy = QProgressBar(self)
        self.busy.setRange(0, 0)  # an endless "working" animation
        self.busy.setTextVisible(False)
        self.busy.setMaximumHeight(6)
        self.busy.setVisible(False)
        self.error = QLabel(self)
        self.error.setWordWrap(True)
        self.error.setStyleSheet(error_style())
        self.error.setVisible(False)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel, self)
        self.sign_in_button = self.buttons.addButton("Sign in", QDialogButtonBox.ButtonRole.AcceptRole)
        self.sign_in_button.setDefault(True)
        self.buttons.accepted.connect(self.submit)  # not accept(): that waits for the server
        self.buttons.rejected.connect(self.reject)
        footer = QVBoxLayout()
        footer.setContentsMargins(16, 8, 16, 0)
        for widget in (self.busy, self.error, self.buttons):
            footer.addWidget(widget)
        layout.addLayout(footer)

        (self.username if self.server.text() else self.server).setFocus()

    # ------------------------------------------------------------------ actions

    def submit(self) -> None:
        """Start signing in on a background thread; the dialog closes when it succeeds."""
        if self.task is not None:
            return
        base_url = normalize_server(self.server.text())
        username = self.username.text().strip()
        if not base_url:
            self.show_error("Enter the address of your EvilFlowers site.")
            return
        if not username or not self.password.text():
            self.show_error("Enter your username and password.")
            return
        self.server.setText(base_url)
        self.set_busy(True)
        # The partial is the password's only holder from here on; Task drops it after the call.
        self.task = Task(
            functools.partial(_sign_in, base_url, username, self.password.text(), self._cancelled),
            describe_sign_in_error,
        )
        self.task.succeeded.connect(lambda key: self._signed_in(base_url, key, username))
        self.task.failed.connect(self._failed)
        self.task.start()

    def _signed_in(self, base_url: str, key: ApiKeyCreated, username: str) -> None:
        self.task = None
        if self._cancelled.is_set():
            # Cancelled after the task's own check: nobody will store this key, so revoke it.
            run_task(functools.partial(revoke_key, base_url, key.token, str(key.id)))
            return
        self.outcome = (base_url, key, username)
        self.accept()

    def _failed(self, message: str) -> None:
        self.task = None
        if self._cancelled.is_set():
            return
        self.set_busy(False)
        self.show_error(message)
        self.password.setFocus()
        self.password.selectAll()

    def set_busy(self, busy: bool) -> None:
        for widget in (self.server, self.username, self.password, self.sign_in_button):
            widget.setEnabled(not busy)
        self.busy.setVisible(busy)
        self.sign_in_button.setText("Signing in…" if busy else "Sign in")
        if busy:
            self.error.setVisible(False)

    def show_error(self, message: str) -> None:
        self.error.setStyleSheet(error_style())
        self.error.setText(message)
        self.error.setVisible(True)

    def done(self, result: int) -> None:
        """Every way out (Sign in, Cancel, Esc, the close button) ends here: forget the password."""
        if self.task is not None:
            self._cancelled.set()  # a key that arrives now is revoked by the task itself
        self.password.clear()
        super().done(result)


def sign_in_dialog(parent: QWidget | None = None, base_url: str = "") -> SignInResult | None:
    """Show the sign-in dialog; return ``(base_url, key, username)``, or None when cancelled."""
    dialog = SignInDialog(parent, base_url)
    try:
        if dialog.exec() == QDialog.DialogCode.Accepted:
            return dialog.outcome
        return None
    finally:
        dialog.outcome = None
        dialog.deleteLater()
