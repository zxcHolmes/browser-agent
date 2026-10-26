"""Timezone-aware, minute-precision platform scheduling."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from .automation import AutomationError

ZONE = ZoneInfo('Asia/Shanghai')


def schedule_time(value, *, now=None, min_lead=120, max_days=None):
    if not value:
        return None
    try:
        date = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if date.tzinfo is None:
            date = date.replace(tzinfo=ZONE)
        date = date.astimezone(ZONE)
        if date.second or date.microsecond:
            raise ValueError('time must have minute precision')
        current = now or datetime.now(ZONE)
        if date < current + timedelta(seconds=min_lead):
            raise ValueError(f'must be at least {min_lead // 60} minutes in the future')
        if max_days and date > current + timedelta(days=max_days):
            raise ValueError(f'must be within {max_days} days')
        return date
    except (ValueError, TypeError) as error:
        raise AutomationError('INVALID_SCHEDULE', f'Invalid --schedule-at: {error}; use YYYY-MM-DDTHH:MM:00+08:00 (Asia/Shanghai)', exit_code=2) from error


def display_time(value):
    return datetime.fromisoformat(value).astimezone(ZONE).strftime('%Y-%m-%d %H:%M')
