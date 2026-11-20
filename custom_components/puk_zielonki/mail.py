"""Blocking IMAP access (run in the executor)."""

from __future__ import annotations

from collections.abc import Iterable
import imaplib
import logging
import re
import ssl

_LOGGER = logging.getLogger(__name__)

_LIST_RE = re.compile(rb'\((?P<flags>[^)]*)\) "(?P<delim>[^"]*)" (?P<name>.+)$')


class MailError(Exception):
    """IMAP connection or protocol problem."""


class MailAuthError(MailError):
    """Login rejected."""


def _quote(folder: str) -> str:
    if folder.startswith('"'):
        return folder
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


class MailClient:
    """Minimal read-only IMAP client for fetching invoice messages."""

    def __init__(self, server: str, port: int, username: str, password: str) -> None:
        self._server = server
        self._port = port
        self._username = username
        self._password = password
        self._imap: imaplib.IMAP4_SSL | None = None
        self.folder: str | None = None

    def __enter__(self) -> MailClient:
        try:
            self._imap = imaplib.IMAP4_SSL(
                self._server, self._port, ssl_context=ssl.create_default_context(), timeout=30
            )
        except (TimeoutError, OSError, imaplib.IMAP4.error) as err:
            raise MailError(f"cannot connect to {self._server}: {err}") from err
        try:
            self._imap.login(self._username, self._password)
        except imaplib.IMAP4.error as err:
            self._close()
            raise MailAuthError(str(err)) from err
        return self

    def __exit__(self, *exc: object) -> None:
        self._close()

    def _close(self) -> None:
        if self._imap is None:
            return
        try:
            if self._imap.state == "SELECTED":
                self._imap.close()
            self._imap.logout()
        except (OSError, imaplib.IMAP4.error):
            pass
        self._imap = None

    @property
    def imap(self) -> imaplib.IMAP4_SSL:
        assert self._imap is not None
        return self._imap

    def _all_mail_folder(self) -> str:
        typ, lines = self.imap.list()
        if typ == "OK":
            for line in lines:
                m = _LIST_RE.match(line) if isinstance(line, bytes) else None
                if m and b"\\All" in m["flags"].split():
                    return m["name"].decode()
        return "INBOX"

    def select(self, folder: str) -> str:
        """Select a folder read-only (auto-detect "All Mail" if empty)."""
        name = folder or self._all_mail_folder()
        typ, data = self.imap.select(_quote(name), readonly=True)
        if typ != "OK":
            raise MailError(f"cannot select folder {name}: {data}")
        self.folder = name.strip('"')
        return self.folder

    def uidvalidity(self) -> int | None:
        _typ, data = self.imap.response("UIDVALIDITY")
        try:
            return int(data[0]) if data and data[0] else None
        except (TypeError, ValueError):
            return None

    def search_from(self, sender: str) -> list[int]:
        try:
            typ, data = self.imap.uid("SEARCH", None, "FROM", _quote(sender))
        except imaplib.IMAP4.error as err:
            raise MailError(f"search failed: {err}") from err
        if typ != "OK":
            raise MailError(f"search failed: {data}")
        return sorted(int(x) for x in (data[0] or b"").split())

    def fetch(self, uids: Iterable[int]) -> dict[int, bytes]:
        """Fetch full messages without setting the \\Seen flag."""
        result: dict[int, bytes] = {}
        for uid in uids:
            try:
                typ, data = self.imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
            except imaplib.IMAP4.error as err:
                raise MailError(f"fetch {uid} failed: {err}") from err
            if typ != "OK":
                raise MailError(f"fetch {uid} failed: {data}")
            for item in data:
                if isinstance(item, tuple):
                    result[uid] = item[1]
                    break
        return result
