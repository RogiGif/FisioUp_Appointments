from datetime import datetime, timedelta

from django.utils import timezone

from core.models import (
    WeeklySchedule,
    WeeklyWorkingBlock,
    WeeklyBreakBlock,
)


def get_active_weekly_schedule(professional):
    if not professional:
        return None
    return (
        WeeklySchedule.objects
        .filter(professional=professional, is_active=True)
        .first()
    )


def _subtract_breaks(blocks, breaks):
    if not breaks:
        return blocks
    intervals = list(blocks)
    for b_start, b_end in breaks:
        next_intervals = []
        for start, end in intervals:
            if b_end <= start or b_start >= end:
                next_intervals.append((start, end))
                continue
            if b_start > start:
                next_intervals.append((start, b_start))
            if b_end < end:
                next_intervals.append((b_end, end))
        intervals = next_intervals
    return intervals


def get_working_blocks(professional, date_obj):
    if not professional:
        return []
    weekday = date_obj.weekday()
    schedule = get_active_weekly_schedule(professional)
    if not schedule or not schedule.is_active:
        return []
    blocks = list(
        WeeklyWorkingBlock.objects
        .filter(weekly_schedule=schedule, weekday=weekday)
        .order_by("start_time")
        .values_list("start_time", "end_time")
    )
    breaks = list(
        WeeklyBreakBlock.objects
        .filter(weekly_schedule=schedule, weekday=weekday)
        .order_by("start_time")
        .values_list("start_time", "end_time")
    )
    return _subtract_breaks(blocks, breaks)


def get_working_weekdays(professional):
    if not professional:
        return []
    schedule = get_active_weekly_schedule(professional)
    if not schedule or not schedule.is_active:
        return []
    return list(
        WeeklyWorkingBlock.objects
        .filter(weekly_schedule=schedule)
        .values_list("weekday", flat=True)
        .distinct()
        .order_by("weekday")
    )


def get_last_end_time_for_date(professional, date_obj):
    blocks = get_working_blocks(professional, date_obj)
    if not blocks:
        return None
    return max(end for _, end in blocks)


def is_time_in_working_blocks(professional, date_obj, time_obj):
    blocks = get_working_blocks(professional, date_obj)
    for start, end in blocks:
        if start <= time_obj < end:
            return True
    return False


def _time_range(start, end, step_minutes):
    current = datetime.combine(datetime.today().date(), start)
    end_dt = datetime.combine(datetime.today().date(), end)
    step = timedelta(minutes=step_minutes)
    while current < end_dt:
        yield current.time().replace(second=0, microsecond=0)
        current += step


def _normalize_occupied_intervals(occupied_intervals):
    normalized = []
    base_date = datetime.today().date()
    for interval in occupied_intervals or []:
        if not interval or len(interval) != 2:
            continue
        start_time, end_time = interval
        if not start_time or not end_time:
            continue
        start_dt = datetime.combine(base_date, start_time)
        end_dt = datetime.combine(base_date, end_time)
        if end_dt <= start_dt:
            continue
        normalized.append((start_dt, end_dt))
    return normalized


def build_slots(
    professional,
    date_obj,
    service_duration_minutes,
    existing_appointments=None,
    blocked_slots=None,
    occupied_intervals=None,
    hard_blocked_intervals=None,
    slot_step_minutes=None,
    simultaneous_capacity=1,
):
    if not professional:
        return []
    today = timezone.localdate()
    now_t = timezone.localtime().time()
    if date_obj < today:
        return []

    taken = set(existing_appointments or [])
    blocked = set(blocked_slots or [])
    occupied = _normalize_occupied_intervals(occupied_intervals)
    hard_blocked = _normalize_occupied_intervals(hard_blocked_intervals)
    step_minutes = slot_step_minutes or service_duration_minutes
    capacity = max(simultaneous_capacity or 1, 1)

    slots = []
    seen = set()
    for start, end in get_working_blocks(professional, date_obj):
        block_end = datetime.combine(datetime.today().date(), end)
        for t in _time_range(start, end, step_minutes=step_minutes):
            if date_obj == today and t <= now_t:
                continue
            if t in taken or t in blocked:
                continue
            slot_start = datetime.combine(datetime.today().date(), t)
            slot_end = slot_start + timedelta(minutes=service_duration_minutes)
            if slot_end > block_end:
                continue
            if hard_blocked:
                if any(slot_start < occ_end and occ_start < slot_end for occ_start, occ_end in hard_blocked):
                    continue
            if occupied:
                overlap_count = sum(
                    1
                    for occ_start, occ_end in occupied
                    if slot_start < occ_end and occ_start < slot_end
                )
                if overlap_count >= capacity:
                    continue
            time_str = t.strftime("%H:%M")
            if time_str not in seen:
                seen.add(time_str)
                slots.append(time_str)
    return slots
