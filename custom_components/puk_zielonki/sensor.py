"""Sensors per property: meter readings and the latest invoice.

One device per property (customer account, e.g. ABC_12). Properties are
discovered from the invoices; a new one gets its entities as soon as its
first invoice is parsed, without reloading the integration.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, METER_GARDEN, METER_MAIN
from .coordinator import PropertyData, PukConfigEntry, PukCoordinator
from .parser import as_date
from .statistics import account_slug, statistic_id


@dataclass(frozen=True, kw_only=True)
class PukSensorDescription(SensorEntityDescription):
    value_fn: Callable[[PropertyData], Any]
    attrs_fn: Callable[[PropertyData], dict[str, Any] | None] = lambda _: None
    exists_fn: Callable[[PropertyData], bool] = lambda _: True


def _meter_value(meter: str) -> Callable[[PropertyData], float | None]:
    def fn(prop: PropertyData) -> float | None:
        hit = prop.meters.get(meter)
        return hit[1].current if hit else None

    return fn


def _meter_attrs(meter: str) -> Callable[[PropertyData], dict[str, Any] | None]:
    def fn(prop: PropertyData) -> dict[str, Any] | None:
        hit = prop.meters.get(meter)
        if not hit:
            return None
        inv, r = hit
        history = [
            {"date": x.period_end, "reading": x.current, "consumption": x.consumption}
            for i in prop.invoices
            for x in i.readings
            if x.meter == meter
        ]
        return {
            "reading_date": r.period_end,
            "period_start": r.period_start,
            "previous_reading": r.previous,
            "consumption": r.consumption,
            "invoice": inv.number,
            "statistic_id": statistic_id(prop.account, f"{meter}_meter"),
            "history": history[-12:],
        }

    return fn


def _invoice_attrs(prop: PropertyData) -> dict[str, Any] | None:
    inv = prop.latest_invoice
    if inv is None:
        return None
    return {
        "number": inv.number,
        "issue_date": inv.issue_date,
        "due_date": inv.due_date,
        "attachment": inv.attachment,
        "received": inv.received,
        "account": prop.account,
        "object": prop.object_id,
        "address": prop.address,
        "invoices_found": len(prop.invoices),
        "statistic_id": statistic_id(prop.account, "cost"),
    }


def _main_reading_date(prop: PropertyData) -> date | None:
    hit = prop.meters.get(METER_MAIN)
    return as_date(hit[1].period_end) if hit else None


SENSORS: tuple[PukSensorDescription, ...] = (
    PukSensorDescription(
        key="main_meter",
        translation_key="main_meter",
        device_class=SensorDeviceClass.WATER,
        # No state_class on purpose: long-term statistics are imported by
        # statistics.py with the real reading dates (see puk_zielonki:* ids).
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        suggested_display_precision=0,
        value_fn=_meter_value(METER_MAIN),
        attrs_fn=_meter_attrs(METER_MAIN),
    ),
    PukSensorDescription(
        key="garden_meter",
        translation_key="garden_meter",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        suggested_display_precision=0,
        value_fn=_meter_value(METER_GARDEN),
        attrs_fn=_meter_attrs(METER_GARDEN),
        exists_fn=lambda prop: METER_GARDEN in prop.meters,
    ),
    PukSensorDescription(
        key="reading_date",
        translation_key="reading_date",
        device_class=SensorDeviceClass.DATE,
        value_fn=_main_reading_date,
    ),
    PukSensorDescription(
        key="invoice_amount",
        translation_key="invoice_amount",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="PLN",
        suggested_display_precision=2,
        value_fn=lambda p: p.latest_invoice.amount if p.latest_invoice else None,
        attrs_fn=_invoice_attrs,
    ),
    PukSensorDescription(
        key="due_date",
        translation_key="due_date",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda p: as_date(p.latest_invoice.due_date) if p.latest_invoice else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PukConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    added: set[tuple[str, str]] = set()

    @callback
    def _add_new() -> None:
        new = []
        for account, prop in coordinator.data.properties.items():
            for desc in SENSORS:
                if (account, desc.key) not in added and desc.exists_fn(prop):
                    added.add((account, desc.key))
                    new.append(PukSensor(coordinator, account, desc))
        if new:
            async_add_entities(new)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class PukSensor(CoordinatorEntity[PukCoordinator], SensorEntity):
    """A value derived from one property's cached invoices."""

    _attr_has_entity_name = True
    entity_description: PukSensorDescription

    def __init__(
        self, coordinator: PukCoordinator, account: str, description: PukSensorDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._account = account
        slug = account_slug(account)
        entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"{entry_id}_{slug}_{description.key}"
        self.entity_id = f"sensor.puk_zielonki_{slug}_{description.key}"
        prop = coordinator.data.properties[account]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_{slug}")},
            name=f"PUK Zielonki {account}",
            manufacturer="Przedsiębiorstwo Usług Komunalnych w Zielonkach",
            model=prop.address,
            serial_number=prop.object_id,
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://puk-zielonki.pl",
        )

    @property
    def _prop(self) -> PropertyData | None:
        return self.coordinator.data.properties.get(self._account)

    @property
    def available(self) -> bool:
        return super().available and self._prop is not None

    @property
    def native_value(self) -> Any:
        prop = self._prop
        return self.entity_description.value_fn(prop) if prop else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        prop = self._prop
        return self.entity_description.attrs_fn(prop) if prop else None
