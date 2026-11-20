"""Long-term statistics dated by the invoice reading date, not the e-mail date.

The invoice e-mail arrives about a week after the meter is read, so the sensor
entities do not feed the recorder statistics themselves (no state_class).
Instead every reading period from every cached invoice is written as an
external statistic (``puk_zielonki:*``) at 12:00 local time on the period's
end date, with daily rows in between that spread the consumption evenly
(estimated). Rows are rebuilt and upserted on each refresh, so the history
also covers invoices that predate the integration.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
import logging

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util, slugify
from homeassistant.util.unit_conversion import VolumeConverter

from .const import DOMAIN, METER_GARDEN, METER_MAIN
from .parser import Invoice, Reading

_LOGGER = logging.getLogger(__name__)

READING_HOUR = time(12, 0)

METER_NAMES = {
    METER_MAIN: "licznik główny",
    METER_GARDEN: "licznik ogrodowy",
}


def account_slug(account: str) -> str:
    """ABC_M3/1 -> abc_m3_1 (usable in entity and statistic ids)."""
    return slugify(account, separator="_") or "unknown"


def statistic_id(account: str, kind: str) -> str:
    """kind: "main_meter", "garden_meter" or "cost"."""
    return f"{DOMAIN}:{account_slug(account)}_{kind}"


def _at(day: str | date, tz: tzinfo) -> datetime:
    d = date.fromisoformat(day) if isinstance(day, str) else day
    return datetime.combine(d, READING_HOUR, tzinfo=tz)


@dataclass
class _Anchor:
    """A known point: cumulative sum on a reading day.

    ``base`` is the meter state at the start of the segment that ends here
    (None for statistics without a meaningful state, e.g. cost).
    """

    day: date
    sum: float
    state: float | None = None
    base: float | None = None


def _spread_daily(anchors: list[_Anchor], tz: tzinfo) -> list[StatisticData]:
    """One row per day at READING_HOUR, linearly interpolated between anchors.

    The anchors themselves keep their exact values; days in between get an
    even share of the segment's consumption, so monthly bars are proportional
    to the number of days instead of showing everything on the reading day.
    """
    rows: list[StatisticData] = []
    prev: _Anchor | None = None
    for a in anchors:
        if prev is not None:
            days = (a.day - prev.day).days
            for i in range(1, days):
                total = prev.sum + (a.sum - prev.sum) * i / days
                row = StatisticData(
                    start=_at(prev.day + timedelta(days=i), tz), sum=round(total, 3)
                )
                if a.base is not None:
                    row["state"] = round(a.base + (total - prev.sum), 3)
                rows.append(row)
        row = StatisticData(start=_at(a.day, tz), sum=round(a.sum, 3))
        if a.state is not None:
            row["state"] = a.state
        rows.append(row)
        prev = a
    return rows


def meter_rows(invoices: Iterable[Invoice], meter: str, tz: tzinfo) -> list[StatisticData]:
    """state = meter reading, sum = cumulative consumption since the first invoice.

    The sum grows by the difference to the last known reading (so a missing
    invoice does not lose consumption); a meter replacement (period starting
    below the last reading) adds only that period's consumption, never a drop.
    Days between readings are interpolated (see _spread_daily).
    """
    periods: dict[tuple[str, float, float], Reading] = {}
    for inv in invoices:
        for r in inv.readings:
            if r.meter == meter:
                periods[(r.period_end, r.previous, r.current)] = r
    if not periods:
        return []
    ordered = sorted(periods.values(), key=lambda r: (r.period_end, r.current))

    first = ordered[0]
    start_day = date.fromisoformat(first.period_start) - timedelta(days=1)
    by_day: dict[date, _Anchor] = {start_day: _Anchor(start_day, 0.0, first.previous)}
    total = 0.0
    last = first.previous
    for r in ordered:
        # Same meter (also across a missing invoice): count from the last known
        # reading. Lower start value = replaced meter: count only this period.
        continuous = r.previous >= last
        delta = r.current - last if continuous else r.consumption
        base = last if continuous else r.previous
        total += max(delta, 0.0)
        last = r.current
        day = date.fromisoformat(r.period_end)
        if day in by_day:
            # Two periods ending the same day (tariff change): keep the first base.
            base = by_day[day].base if by_day[day].base is not None else base
        by_day[day] = _Anchor(day, total, r.current, base)
    return _spread_daily([by_day[d] for d in sorted(by_day)], tz)


def cost_rows(invoices: Iterable[Invoice], tz: tzinfo) -> list[StatisticData]:
    """Cumulative invoice amounts, spread over the days the invoices cover.

    An invoice is anchored on its last reading date (fallback: issue date);
    its amount is spread from the previous invoice's anchor (for the first
    invoice: the start of its billing period).
    """
    items: list[tuple[date, float, date | None]] = []
    for inv in invoices:
        if inv.amount is None:
            continue
        ends = [r.period_end for r in inv.readings]
        day = max(ends) if ends else inv.issue_date
        if not day:
            continue
        starts = [r.period_start for r in inv.readings]
        first_day = date.fromisoformat(min(starts)) - timedelta(days=1) if starts else None
        items.append((date.fromisoformat(day), inv.amount, first_day))
    if not items:
        return []
    items.sort()
    start = items[0][2] or items[0][0]
    by_day: dict[date, _Anchor] = {start: _Anchor(start, 0.0)}
    total = 0.0
    for day, amount, _ in items:
        total += amount
        by_day[day] = _Anchor(day, round(total, 2))
    return _spread_daily([by_day[d] for d in sorted(by_day)], tz)


def async_update_statistics(hass: HomeAssistant, account: str, invoices: list[Invoice]) -> None:
    """Upsert all external statistics of one property from its cached invoices."""
    tz = dt_util.get_default_time_zone()
    for meter, name in METER_NAMES.items():
        rows = meter_rows(invoices, meter, tz)
        if not rows:
            continue
        async_add_external_statistics(
            hass,
            StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=f"PUK Zielonki {account} {name}",
                source=DOMAIN,
                statistic_id=statistic_id(account, f"{meter}_meter"),
                unit_class=VolumeConverter.UNIT_CLASS,
                unit_of_measurement=UnitOfVolume.CUBIC_METERS,
            ),
            rows,
        )
        _LOGGER.debug(
            "%s: %d statistic rows, last %s",
            statistic_id(account, f"{meter}_meter"),
            len(rows),
            rows[-1],
        )
    rows = cost_rows(invoices, tz)
    if rows:
        async_add_external_statistics(
            hass,
            StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=f"PUK Zielonki {account} koszt",
                source=DOMAIN,
                statistic_id=statistic_id(account, "cost"),
                unit_class=None,
                unit_of_measurement="PLN",
            ),
            rows,
        )
