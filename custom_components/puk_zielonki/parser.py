"""Parse PUK Zielonki invoice e-mails and their PDF attachment.

Kept free of Home Assistant imports so it can be tested standalone.

Invoice table rows (pypdf plain-text extraction) look like::

    Woda  47  231,71 4,9315-07-26 - 17-09-26 331 - 378 8%m3 47
    Woda  29  142,97 4,9315-07-26 - 17-09-26 1487 - 1563 8%m3 76  47
    Ścieki  29  239,54 8,2615-07-26 - 17-09-26 1487 - 1563 8%m3 76  47

The unit price is glued to the period start date, so the regex anchors on the
"dd-mm-yy - dd-mm-yy  N - M" part. The main meter is the one that also appears
on the "Ścieki" (sewage) rows; a "Woda"-only range is the garden sub-meter
(it is missing on winter invoices when the garden meter is not read).
"""

from __future__ import annotations

import contextlib
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
import email
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
import io
import re
from typing import Any

from .const import METER_GARDEN, METER_MAIN

_ROW_RE = re.compile(
    r"^(?P<service>Woda|Ścieki)\b.*?"
    r"(?P<start>\d{2}-\d{2}-\d{2}) - (?P<end>\d{2}-\d{2}-\d{2})\s+"
    r"(?P<prev>\d+(?:,\d+)?) - (?P<curr>\d+(?:,\d+)?)",
    re.MULTILINE,
)
_INVOICE_NO_RE = re.compile(r"FAKTURA VAT NR\s*(FV/\d+/\d{4})")
_ISSUE_RE = re.compile(r"Data wystawienia:\s*(\d{2}-\d{2}-\d{4})")
_DUE_RE = re.compile(
    r"(\d{2}-\d{2}-\d{4})\s*Termin płatności:|Termin płatności:\s*(\d{2}-\d{2}-\d{4})"
)
_TOTAL_PDF_RE = re.compile(r"([\d ]+,\d{2})\s*zł\s*Do zapłaty:|Do zapłaty:\s*([\d ]+,\d{2})\s*zł")

# Values from the notification e-mail body (more reliable than the PDF layout).
_BODY_AMOUNT_RE = re.compile(r"Kwota do zapłaty wynosi:\s*([\d ]+,\d{2})")
_BODY_NUMBER_RE = re.compile(r"Numer faktury:\s*(FV/\d+/\d{4})")
_BODY_ISSUE_RE = re.compile(r"Data wystawienia:\s*(\d{2}\.\d{2}\.\d{4})")
_BODY_DUE_RE = re.compile(r"Termin płatności:\s*(\d{2}\.\d{2}\.\d{4})")

# Property (customer account = "numer ewidencyjny", e.g. ABC_12 or ABC_M3/1).
_BODY_ACCOUNT_RE = re.compile(r"numer ewidencyjny:\s*(\S+)")
_PDF_ACCOUNT_RES = (
    re.compile(r"NABYWCA:\s*(\S+)"),
    re.compile(r"Nazwa zleceniodawcy \[([^\]]+)\]"),
)
# pypdf plain text: "NRB:\n<owner>\n<address>\n<object> (G)OBIEKT:"
_PDF_OBJECT_RE = re.compile(
    r"NRB:\n[^\n]*\n(?P<address>[^\n]+)\n(?P<object>[^\n]+?)\s*OBIEKT:"
    r"|OBIEKT:\s*(?P<object2>[^\n]+)\n[^\n]*\n(?P<address2>[^\n]+)"
)
UNKNOWN_ACCOUNT = "nieznany"


class ParseError(Exception):
    """The invoice could not be parsed."""


@dataclass
class Reading:
    """One meter reading period from an invoice."""

    meter: str
    period_start: str  # ISO date
    period_end: str  # ISO date (reading date)
    previous: float
    current: float

    @property
    def consumption(self) -> float:
        return round(self.current - self.previous, 3)


@dataclass
class Invoice:
    """A parsed invoice."""

    uid: int
    number: str | None
    issue_date: str | None
    due_date: str | None
    amount: float | None
    received: str | None
    attachment: str | None
    readings: list[Reading] = field(default_factory=list)
    account: str = UNKNOWN_ACCOUNT
    object_id: str | None = None
    address: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Invoice:
        data = dict(data)
        data["readings"] = [Reading(**r) for r in data.get("readings", [])]
        return cls(**data)

    def latest_reading(self, meter: str) -> Reading | None:
        rows = [r for r in self.readings if r.meter == meter]
        return max(rows, key=lambda r: (r.period_end, r.current), default=None)


def _num(value: str) -> float:
    return float(value.replace(" ", "").replace(",", "."))


def _date(value: str, fmt: str) -> str:
    return datetime.strptime(value, fmt).date().isoformat()


def _decode(value: str | None) -> str:
    return str(make_header(decode_header(value))) if value else ""


def pdf_text(data: bytes) -> str:
    """Extract plain text from all pages of a PDF."""
    from pypdf import PdfReader  # noqa: PLC0415 - heavy import, only in executor

    reader = PdfReader(io.BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def parse_readings(text: str) -> list[Reading]:
    """Extract meter readings from invoice text."""
    water: dict[tuple[str, str, str, str], None] = {}
    sewage: set[tuple[str, str, str, str]] = set()
    for m in _ROW_RE.finditer(text):
        key = (m["start"], m["end"], m["prev"], m["curr"])
        if m["service"] == "Woda":
            water[key] = None
        else:
            sewage.add(key)
    readings = []
    for key in water:
        start, end, prev, curr = key
        # Main meter = also billed for sewage. With no sewage rows at all (e.g.
        # a plot with water only) every meter on the invoice is treated as main.
        main = key in sewage or not sewage
        readings.append(
            Reading(
                meter=METER_MAIN if main else METER_GARDEN,
                period_start=_date(start, "%d-%m-%y"),
                period_end=_date(end, "%d-%m-%y"),
                previous=_num(prev),
                current=_num(curr),
            )
        )
    return readings


def parse_message(uid: int, raw: bytes) -> Invoice:
    """Parse one raw RFC822 message into an Invoice."""
    msg: Message = email.message_from_bytes(raw)
    body = ""
    pdf: bytes | None = None
    attachment: str | None = None
    for part in msg.walk():
        filename = part.get_filename()
        ctype = part.get_content_type()
        if filename or ctype == "application/pdf":
            name = _decode(filename) if filename else "faktura.pdf"
            if pdf is None and (ctype == "application/pdf" or name.lower().endswith(".pdf")):
                pdf = part.get_payload(decode=True)
                attachment = name
        elif ctype == "text/plain" and not body:
            payload = part.get_payload(decode=True) or b""
            body = payload.decode(part.get_content_charset() or "utf-8", "replace")

    if pdf is None:
        raise ParseError(f"message {uid} has no PDF attachment")
    text = pdf_text(pdf)
    readings = parse_readings(text)
    if not readings:
        raise ParseError(f"no meter readings found in {attachment}")

    return _build_invoice(uid, msg, body, text, attachment, readings)


def _first(*matches: re.Match[str] | None) -> str | None:
    """First non-empty group of the first match."""
    for m in matches:
        if m:
            return next(g for g in m.groups() if g)
    return None


def _build_invoice(
    uid: int,
    msg: Message,
    body: str,
    text: str,
    attachment: str | None,
    readings: list[Reading],
) -> Invoice:
    """Invoice fields: the e-mail body first, the PDF text as a fallback."""
    first = _first
    number = first(_BODY_NUMBER_RE.search(body), _INVOICE_NO_RE.search(text))
    if m := _BODY_ISSUE_RE.search(body):
        issue = _date(m[1], "%d.%m.%Y")
    elif s := first(_ISSUE_RE.search(text)):
        issue = _date(s, "%d-%m-%Y")
    else:
        issue = None
    if m := _BODY_DUE_RE.search(body):
        due = _date(m[1], "%d.%m.%Y")
    elif s := first(_DUE_RE.search(text)):
        due = _date(s, "%d-%m-%Y")
    else:
        due = None
    amount_s = first(_BODY_AMOUNT_RE.search(body), _TOTAL_PDF_RE.search(text))

    if m := _BODY_ACCOUNT_RE.search(body):
        account = m[1]
    else:
        account = first(*(r.search(text) for r in _PDF_ACCOUNT_RES)) or UNKNOWN_ACCOUNT
    object_id = address = None
    if m := _PDF_OBJECT_RE.search(text):
        object_id = (m["object"] or m["object2"] or "").strip() or None
        address = (m["address"] or m["address2"] or "").strip() or None

    received = None
    if msg["Date"]:
        with contextlib.suppress(TypeError, ValueError):
            received = parsedate_to_datetime(msg["Date"]).isoformat()

    return Invoice(
        uid=uid,
        number=number,
        issue_date=issue,
        due_date=due,
        amount=_num(amount_s) if amount_s else None,
        received=received,
        attachment=attachment,
        readings=readings,
        account=account,
        object_id=object_id,
        address=address,
    )


def latest_by_meter(invoices: list[Invoice]) -> dict[str, tuple[Invoice, Reading]]:
    """Newest reading of each meter across all invoices."""
    best: dict[str, tuple[Invoice, Reading]] = {}
    for inv in invoices:
        for meter in (METER_MAIN, METER_GARDEN):
            r = inv.latest_reading(meter)
            if r is None:
                continue
            cur = best.get(meter)
            if cur is None or (r.period_end, inv.uid) > (cur[1].period_end, cur[0].uid):
                best[meter] = (inv, r)
    return best


def as_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None
