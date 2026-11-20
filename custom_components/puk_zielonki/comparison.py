"""Compare invoice readings with a reference sensor (e.g. an ESPHome pulse meter).

For every reading on the invoices the reference sensor's long-term statistics
are looked up at READING_HOUR on the reading date (the invoice has no time of
day). Two numbers come out of it:

- the offset: sensor value minus invoice reading (calibration drift; the
  invoice reading is a whole number, so ~1 m³ is within tolerance);
- the period consumption from both sources, which does not depend on the
  sensor's calibration offset.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, tzinfo
import logging
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .parser import Invoice
from .statistics import READING_HOUR

_LOGGER = logging.getLogger(__name__)

# How far back before READING_HOUR a statistics row may be (sensor offline).
_LOOKBACK = timedelta(hours=12)


@dataclass
class ComparisonRow:
    """One reading date."""

    date: str
    invoice: float
    sensor: float
    offset: float
    invoice_consumption: float | None = None
    sensor_consumption: float | None = None
    consumption_diff: float | None = None
    consumption_diff_pct: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class Comparison:
    """Comparison of one meter with its reference sensor."""

    entity_id: str
    rows: list[ComparisonRow]

    @property
    def latest(self) -> ComparisonRow | None:
        return self.rows[-1] if self.rows else None


def meter_readings(invoices: list[Invoice], meter: str) -> dict[date, float]:
    """Reading date -> invoice reading (the later one if two end on the same day)."""
    result: dict[date, float] = {}
    for inv in invoices:
        for r in inv.readings:
            if r.meter == meter:
                day = date.fromisoformat(r.period_end)
                result[day] = max(result.get(day, r.current), r.current)
    return dict(sorted(result.items()))


def build_rows(readings: dict[date, float], sensor: dict[date, float]) -> list[ComparisonRow]:
    """Join invoice readings with sensor values on the same dates."""
    rows: list[ComparisonRow] = []
    prev: ComparisonRow | None = None
    for day, invoice in readings.items():
        value = sensor.get(day)
        if value is None:
            prev = None  # no sensor data: don't compare a period across the gap
            continue
        row = ComparisonRow(
            date=day.isoformat(),
            invoice=invoice,
            sensor=round(value, 3),
            offset=round(value - invoice, 3),
        )
        if prev is not None:
            inv_c = round(invoice - prev.invoice, 3)
            sen_c = round(value - prev.sensor, 3)
            row.invoice_consumption = inv_c
            row.sensor_consumption = sen_c
            row.consumption_diff = round(sen_c - inv_c, 3)
            if inv_c > 0:
                row.consumption_diff_pct = round((sen_c - inv_c) / inv_c * 100, 1)
        rows.append(row)
        prev = row
    return rows


def _sensor_values(
    hass: HomeAssistant, entity_id: str, days: list[date], tz: tzinfo
) -> dict[date, float]:
    """Blocking: the sensor's statistics state at READING_HOUR on each day."""
    result: dict[date, float] = {}
    for day in days:
        at = datetime.combine(day, READING_HOUR, tzinfo=tz)
        # An hourly row starting at T holds the state at T+1h.
        stats = statistics_during_period(
            hass,
            at - _LOOKBACK,
            at,
            {entity_id},
            "hour",
            {"volume": UnitOfVolume.CUBIC_METERS},
            {"state"},
        )
        rows = [r for r in stats.get(entity_id, []) if r.get("state") is not None]
        if rows:
            result[day] = float(rows[-1]["state"])
    return result


async def async_compare(
    hass: HomeAssistant, invoices: list[Invoice], meter: str, entity_id: str
) -> Comparison:
    """Compare one meter's invoice readings with a reference sensor."""
    readings = meter_readings(invoices, meter)
    tz = dt_util.get_default_time_zone()
    values = await get_instance(hass).async_add_executor_job(
        _sensor_values, hass, entity_id, list(readings), tz
    )
    rows = build_rows(readings, values)
    if readings and not rows:
        _LOGGER.warning(
            "%s has no long-term statistics on any invoice reading date; "
            "it needs a state_class to be compared",
            entity_id,
        )
    return Comparison(entity_id=entity_id, rows=rows)
