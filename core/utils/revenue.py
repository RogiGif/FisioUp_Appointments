from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.db.models import Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from core.models import Appointment, Professional
from core.permissions import can_view_all_calendar


@dataclass
class Trend:
    current: Decimal
    previous: Decimal
    delta_abs: Decimal
    delta_pct: Decimal | None
    direction: str  # "up" | "down" | "flat"


def get_revenue_queryset(user):
    qs = Appointment.objects.filter(status=Appointment.STATUS_COMPLETED)
    if not can_view_all_calendar(user):
        prof = Professional.objects.filter(user=user).first()
        if prof:
            qs = qs.filter(professional=prof)
    return qs


def _range_sum(qs, start_dt: datetime, end_dt: datetime) -> Decimal:
    total = (
        qs.filter(date__gte=start_dt.date(), date__lt=end_dt.date())
        .aggregate(total=Coalesce(Sum("final_price"), Decimal("0.00")))
        .get("total")
    )
    return total or Decimal("0.00")


def compute_trend(current: Decimal, previous: Decimal) -> Trend:
    delta_abs = current - previous
    if previous and previous != Decimal("0.00"):
        delta_pct = (delta_abs / previous) * Decimal("100")
    else:
        delta_pct = None
    if delta_abs > 0:
        direction = "up"
    elif delta_abs < 0:
        direction = "down"
    else:
        direction = "flat"
    return Trend(current=current, previous=previous, delta_abs=delta_abs, delta_pct=delta_pct, direction=direction)


def month_start(d: date) -> date:
    return d.replace(day=1)


def next_month(d: date) -> date:
    if d.month == 12:
        return d.replace(year=d.year + 1, month=1, day=1)
    return d.replace(month=d.month + 1, day=1)


def month_range(d: date) -> tuple[date, date]:
    start = month_start(d)
    end = next_month(start)
    return start, end


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def week_range(d: date) -> tuple[date, date]:
    start = week_start(d)
    end = start + timedelta(days=7)
    return start, end


def day_range(d: date) -> tuple[date, date]:
    return d, d + timedelta(days=1)

