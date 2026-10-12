"""Read job-alert emails from Gmail over IMAP (read-only; nothing is marked or moved).

Needs a Gmail app password (Google Account > Security > 2-Step Verification >
App passwords), stored as the `gmail-app-password` secret (see secrets.py).
Gmail's X-GM-RAW extension lets IMAP use normal Gmail search syntax.
"""

import email
import imaplib
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from email import policy
from email.utils import parsedate_to_datetime

from . import secrets
from .sources import html_to_text


@dataclass
class Message:
    message_id: str
    sender: str
    subject: str
    received_at: str
    text: str


def _body_text(msg: email.message.EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    content = str(part.get_content())
    return content if part.get_content_subtype() == "plain" else html_to_text(content)


def messages(query: str, user: str, on_total: Callable[[int], None] | None = None) -> Iterator[Message]:
    """Yield messages matching a Gmail search. IMAP errors are raised as RuntimeError.

    `on_total` is called with the number of matching messages before the first one.
    """
    try:
        yield from _messages(query, user, on_total)
    except imaplib.IMAP4.error as e:
        msg = f"Gmail IMAP error: {e}"
        raise RuntimeError(msg) from e


def _messages(query: str, user: str, on_total: Callable[[int], None] | None) -> Iterator[Message]:
    password = secrets.require(
        "gmail-app-password", "Create one at https://myaccount.google.com/apppasswords (needs 2-Step Verification)."
    )
    with imaplib.IMAP4_SSL("imap.gmail.com") as imap:
        # Strip spaces (Google displays app passwords in groups of 4) and any terminal
        # escape sequences that got pasted into secret-tool's prompt.
        password = re.sub(r"\x1b\[[0-9;]*[~A-Za-z]|[\x00-\x1f\s]", "", password)
        imap.login(secrets.get("gmail-user") or user, password)
        imap.select('"[Gmail]/All Mail"', readonly=True)
        quoted = '"' + " ".join(query.split()).replace('"', '\\"') + '"'
        typ, data = imap.uid("SEARCH", "X-GM-RAW", quoted)
        if typ != "OK":
            msg = f"Gmail search failed: {data}"
            raise RuntimeError(msg)
        uids = data[0].split()
        if on_total is not None:
            on_total(len(uids))
        for uid in uids:
            typ, parts = imap.uid("FETCH", uid, "(BODY.PEEK[])")  # PEEK: don't mark as read
            if typ != "OK" or not parts or parts[0] is None:
                continue
            msg = email.message_from_bytes(parts[0][1], policy=policy.default)
            yield Message(
                message_id=(msg["Message-ID"] or f"uid:{uid.decode()}").strip(),
                sender=str(msg["From"] or ""),
                subject=str(msg["Subject"] or ""),
                received_at=_date(msg["Date"]),
                text=_body_text(msg),
            )


def _date(header: object) -> str:
    """ISO date from a Date header, or "" if it's missing or malformed."""
    if header is None:
        return ""
    try:
        return parsedate_to_datetime(str(header)).isoformat()
    except (TypeError, ValueError):
        return ""
