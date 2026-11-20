"""Polls the mailbox for new PUK Zielonki invoices."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .comparison import Comparison, async_compare
from .const import (
    CONF_FOLDER,
    CONF_REFERENCE,
    CONF_SENDER,
    DOMAIN,
    METER_GARDEN,
    METER_MAIN,
    STORAGE_VERSION,
    UPDATE_INTERVAL,
)
from .mail import MailAuthError, MailClient, MailError
from .parser import Invoice, ParseError, Reading, latest_by_meter, parse_message
from .statistics import account_slug, async_update_statistics

_LOGGER = logging.getLogger(__name__)

EVENT_NEW_INVOICE = f"{DOMAIN}_new_invoice"


# Bump when parsing changes so cached invoices are downloaded and parsed again.
PARSER_VERSION = 2


@dataclass
class PropertyData:
    """Invoices of one property (customer account / "numer ewidencyjny")."""

    account: str
    invoices: list[Invoice]
    latest_invoice: Invoice | None
    meters: dict[str, tuple[Invoice, Reading]]
    comparisons: dict[str, Comparison] = field(default_factory=dict)

    @property
    def address(self) -> str | None:
        return next((i.address for i in reversed(self.invoices) if i.address), None)

    @property
    def object_id(self) -> str | None:
        return next((i.object_id for i in reversed(self.invoices) if i.object_id), None)


@dataclass
class PukData:
    """What the sensors show."""

    properties: dict[str, PropertyData]
    folder: str | None


type PukConfigEntry = ConfigEntry[PukCoordinator]


def reference_key(account: str, meter: str) -> str:
    """Options key of the reference sensor for one meter of one property."""
    return f"{CONF_REFERENCE}_{account_slug(account)}_{meter}"


class PukCoordinator(DataUpdateCoordinator[PukData]):
    """Keeps a local cache of parsed invoices; only new messages are downloaded."""

    config_entry: PukConfigEntry

    def __init__(self, hass: HomeAssistant, entry: PukConfigEntry) -> None:
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=UPDATE_INTERVAL
        )
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._cache: dict[str, Any] | None = None
        self._stats_written = False
        self._comparisons: dict[tuple[str, str], Comparison] = {}

    async def _load_cache(self) -> dict[str, Any]:
        if self._cache is None:
            self._cache = await self._store.async_load() or {}
            self._cache.setdefault("invoices", {})
            self._cache.setdefault("failed", [])
        return self._cache

    def _poll(self, cache: dict[str, Any]) -> tuple[list[Invoice], str | None]:
        """Blocking: download and parse messages not seen before."""
        cfg = self.config_entry.data
        new: list[Invoice] = []
        with MailClient(
            cfg[CONF_HOST], cfg[CONF_PORT], cfg[CONF_USERNAME], cfg[CONF_PASSWORD]
        ) as client:
            folder = client.select(cfg.get(CONF_FOLDER, ""))
            validity = client.uidvalidity()
            if (
                cache.get("folder") != folder
                or cache.get("uidvalidity") != validity
                or cache.get("parser_version") != PARSER_VERSION
            ):
                if cache["invoices"]:
                    _LOGGER.info("Folder/UIDVALIDITY/parser changed, re-reading all invoices")
                cache.update(
                    folder=folder,
                    uidvalidity=validity,
                    parser_version=PARSER_VERSION,
                    invoices={},
                    failed=[],
                )
            known = set(cache["invoices"]) | {str(u) for u in cache["failed"]}
            uids = [u for u in client.search_from(cfg[CONF_SENDER]) if str(u) not in known]
            if uids:
                _LOGGER.debug("Fetching %d new message(s) from %s", len(uids), folder)
            for uid, raw in client.fetch(uids).items():
                try:
                    inv = parse_message(uid, raw)
                except ParseError as err:
                    # e.g. a reminder / non-invoice message from the same sender
                    _LOGGER.info("Skipping message %s: %s", uid, err)
                    cache["failed"].append(uid)
                    continue
                except Exception:
                    _LOGGER.exception("Failed to parse message %s", uid)
                    cache["failed"].append(uid)
                    continue
                cache["invoices"][str(uid)] = inv.as_dict()
                new.append(inv)
        return new, folder

    async def _async_update_data(self) -> PukData:
        cache = await self._load_cache()
        first_run = not cache["invoices"]
        try:
            new, folder = await self.hass.async_add_executor_job(self._poll, cache)
        except MailAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except MailError as err:
            raise UpdateFailed(str(err)) from err
        await self._store.async_save(cache)

        invoices = sorted(
            (Invoice.from_dict(d) for d in cache["invoices"].values()),
            key=lambda i: (i.issue_date or "", i.uid),
        )
        by_account: dict[str, list[Invoice]] = {}
        for inv in invoices:
            by_account.setdefault(inv.account, []).append(inv)
        properties = {
            account: PropertyData(
                account=account,
                invoices=items,
                latest_invoice=items[-1],
                meters=latest_by_meter(items),
            )
            for account, items in by_account.items()
        }
        if new or not self._stats_written:
            for prop in properties.values():
                async_update_statistics(self.hass, prop.account, prop.invoices)
            self._stats_written = True
        await self._async_update_comparisons(properties, refresh=bool(new))
        if not first_run:
            for inv in new:
                self.hass.bus.async_fire(EVENT_NEW_INVOICE, inv.as_dict())
        return PukData(properties=properties, folder=folder)

    async def _async_update_comparisons(
        self, properties: dict[str, PropertyData], *, refresh: bool
    ) -> None:
        """Compare readings with the configured reference sensors.

        Done once per setup (options changes reload the entry) and again when
        new invoices arrive; the recorder lookups are not repeated every poll.
        """
        options = self.config_entry.options
        for account, prop in properties.items():
            for meter in (METER_MAIN, METER_GARDEN):
                entity_id = options.get(reference_key(account, meter))
                if not entity_id or meter not in prop.meters:
                    continue
                key = (account, meter)
                cached = self._comparisons.get(key)
                if refresh or cached is None or cached.entity_id != entity_id:
                    try:
                        cached = await async_compare(self.hass, prop.invoices, meter, entity_id)
                    except Exception:
                        _LOGGER.exception("Comparing %s with %s failed", meter, entity_id)
                        continue
                    self._comparisons[key] = cached
                prop.comparisons[meter] = cached
