"""Constants for PUK Zielonki."""

from datetime import timedelta

DOMAIN = "puk_zielonki"

CONF_FOLDER = "folder"
CONF_SENDER = "sender"

DEFAULT_SERVER = "imap.gmail.com"
DEFAULT_PORT = 993
DEFAULT_SENDER = "ibo@puk-zielonki.pl"
# Empty folder = auto-detect the IMAP "\All" special-use folder (Gmail's "All Mail"),
# falling back to INBOX. Invoices are usually archived/labelled, not in INBOX.
DEFAULT_FOLDER = ""

UPDATE_INTERVAL = timedelta(hours=6)
STORAGE_VERSION = 1

METER_MAIN = "main"
METER_GARDEN = "garden"
