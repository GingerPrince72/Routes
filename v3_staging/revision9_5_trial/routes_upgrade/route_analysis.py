"""Precise, provider-independent evaluation of static delivery routes.

Units at the boundary are **minutes** for compatibility with earlier releases;
Decimal arithmetic retains Google's sub-minute values throughout planning.
No network activity, caching of provider matrices, or database access occurs here.
"""

from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP
from hashlib import sha256
from itertools import permutations
import json
import math


class RouteDataError(ValueError):
    """Invalid duration, service time, time window, or matrix."""


def minute_value(value):
    """Convert nonnegative finite numeric minutes without rounding to an integer."""
    if isinstance(value, bool):
        raise RouteDataError('Boolean is not a journey duration.')
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RouteDataError('Invalid journey duration.') from exc
    if not result.is_finite() or result < 0:
        raise RouteDataError('Journey duration must be finite and nonnegative.')
    return result


def hhmm_minutes(text):
    """Read a wall-clock time in HH:MM, returning integer minutes."""
    if not isinstance(text, str):
        raise ValueError('Time must use HH:MM format.')
    parts = text.split(':')
    if len(parts) != 2 or any(len(part) != 2 or not part.isascii() or not part.isdigit()
                              for part in parts):
        raise ValueError('Time must use HH:MM format.')
    hour, minute = map(int, parts)
    if not 0 <= hour < 24 or not 0 <= minute < 60:
        raise ValueError('Invalid HH:MM time.')
    return hour * 60 + minute


def as_microseconds(value):
    """Decimal-minute duration to the nearest microsecond for datetime arithmetic."""
    return int((minute_value(value) * 60_000_000).to_integral_value(
        rounding=ROUND_HALF_UP))


def as_ceil_seconds(value):
    """Round up ONLY at the OR-Tools integer-seconds solver boundary."""
    return int((minute_value(value) * 60).to_integral_value(rounding=ROUND_CEILING))


def display_minutes(value):
    """Human-facing value, rounded to three decimal minutes."""
    return float(minute_value(value).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP))


def clock_text(absolute_minutes, seconds=False):
    """Display clock to nearest minute or second, never truncate during optimisation."""
    value = minute_value(absolute_minutes)
    if seconds:
        whole = int((value * 60).to_integral_value(rounding=ROUND_HALF_UP))
        return f'{whole // 3600:02d}:{(whole // 60) % 60:02d}:{whole % 60:02d}'
    whole = int(value.to_integral_value(rounding=ROUND_HALF_UP))
    return f'{whole // 60:02d}:{whole % 60:02d}'


def evaluate_order(order, stops, matrix, departure, max_hours, windows=None):
    """Score a fixed order with unloading, closures and waiting, using Decimal.

    Returns None for an infeasible route; raises on malformed source values.
    The returned internal Decimal values never go directly into JSON.
    """
    n = len(stops)
    if len(order) != n or order[0] != 0 or order[-1] != n - 1:
        raise RouteDataError('Route order must begin and finish at the depot.')
    if len(set(order)) != n:
        raise RouteDataError('Route order contains duplicate nodes.')
    if len(matrix) != n or any(len(row) != n for row in matrix):
        raise RouteDataError('Invalid travel-time matrix dimensions.')

    day_start = Decimal(hhmm_minutes(departure))
    max_minutes = minute_value(max_hours) * 60
    if max_minutes <= 0:
        raise RouteDataError('Maximum route duration must be positive.')
    end_limit = min(Decimal(1439), day_start + max_minutes)
    current = day_start
    driving = Decimal(0)
    service_total = Decimal(0)
    wait_total = Decimal(0)
    windows = windows or {}
    steps = []
    for position, node in enumerate(order):
        drive = Decimal(0) if position == 0 else minute_value(matrix[order[position - 1]][node])
        current += drive
        driving += drive
        service = Decimal(0) if position in (0, n - 1) else minute_value(
            stops[node].get('service_minutes', 5)
            if stops[node].get('service_minutes') is not None else 5)
        wait = Decimal(0)
        if node in windows:
            periods = windows[node]
            if not periods:
                return None
            possible = []
            for start, close in periods:
                opening, closing = minute_value(start), minute_value(close)
                if closing < opening:
                    raise RouteDataError('Opening hours contain a reversed interval.')
                candidate = max(current, opening)
                if candidate + service <= closing:
                    possible.append(candidate)
            if not possible:
                return None
            first_possible = min(possible)
            wait = first_possible - current
            current = first_possible
        arrival = current
        current += service
        service_total += service
        wait_total += wait
        if current > end_limit:
            return None
        steps.append({
            'index': node, 'arrival': arrival, 'departure': current,
            'drive': drive, 'wait': wait, 'service': service,
        })
    return {
        'order': tuple(order), 'steps': steps, 'return_minutes': current,
        'driving_minutes': driving, 'waiting_minutes': wait_total,
        'service_minutes': service_total, 'duration_minutes': current - day_start,
        'score': (current, driving, tuple(order)),
    }


def ranked_orders(stops, matrix, departure, max_hours=12, windows=None):
    """All feasible permutations for at most seven outlets, sorted by exact finish."""
    if len(stops) < 3 or len(stops) - 2 > 7:
        raise RouteDataError('Exhaustive comparison supports one to seven outlets.')
    if any(not periods for periods in (windows or {}).values()):
        return []
    results = []
    for middle in permutations(range(1, len(stops) - 1)):
        candidate = evaluate_order((0,) + middle + (len(stops) - 1,),
                                   stops, matrix, departure, max_hours, windows)
        if candidate is not None:
            results.append(candidate)
    results.sort(key=lambda item: item['score'])
    return results


def snapshot_report(stops, matrix, departure, max_hours=12, windows=None, top_count=5):
    """Small audit summary, NOT a persisted raw Google matrix.

    Includes the best route finishing at each outlet, exposing cases such as
    Krypton-last without any additional Google request.
    """
    results = ranked_orders(stops, matrix, departure, max_hours, windows)
    if not results:
        return {'evaluated_orders': 0, 'top_routes': [], 'best_by_last': [],
                'matrix_digest': None, 'raw_matrix_saved': False}

    def summary(candidate):
        order = candidate['order']
        return {
            'order': [stops[i].get('name', str(i)) for i in order],
            'order_ids': [str(stops[i].get('id', i)) for i in order],
            'return_time': clock_text(candidate['return_minutes']),
            'duration_minutes': display_minutes(candidate['duration_minutes']),
            'driving_minutes': display_minutes(candidate['driving_minutes']),
            'waiting_minutes': display_minutes(candidate['waiting_minutes']),
            'service_minutes': display_minutes(candidate['service_minutes']),
            'last_outlet': stops[order[-2]].get('name', str(order[-2])),
        }

    by_last = {}
    for candidate in results:
        end = candidate['order'][-2]
        by_last.setdefault(end, candidate)
    # A non-reversible, deterministic fingerprint helps compare two tests
    # without writing the provider's raw element values into the journal.
    signature = json.dumps([[str(minute_value(x)) for x in row] for row in matrix],
                           separators=(',', ':'))
    digest = sha256(signature.encode('utf-8')).hexdigest()
    return {
        'evaluated_orders': len(results),
        'top_routes': [summary(x) for x in results[:top_count]],
        'best_by_last': [summary(x) for x in by_last.values()],
        'matrix_digest': digest, 'raw_matrix_saved': False,
        'precision': 'sub-minute values retained; display rounded to 0.001 min',
    }
