"""Budgeted, traffic-dependent evaluation of multiple delivery orders.

This module makes NO network requests itself. Every callback is supplied by the
caller, which must journal a billable attempt before invoking Google.

Two-order mode preserves Revision 9.3 selection. For three/four candidates,
orders are diversified by LAST outlet, so an alternative such as Krypton-last
is not discarded just because it ranks poorly in the initial traffic snapshot.
This remains a bounded comparison, not a global time-dependent optimum.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from route_analysis import (
    RouteDataError, as_microseconds, display_minutes, minute_value, ranked_orders,
)
from route_optimizer import OptimisationError


MAX_TRAFFIC_CANDIDATES = 4


def _static_score(order, stops, matrix, departure, windows, max_hours):
    """Compatibility helper, using the same exact scoring as the optimiser."""
    from route_analysis import evaluate_order
    result = evaluate_order(order, stops, matrix, departure, max_hours, windows)
    return (result['return_minutes'], result['driving_minutes']) if result else None


def candidate_orders(stops, matrix, departure, windows, max_hours=12, count=2):
    """Feasible candidate orders ranked by precise static snapshot.

    For count>=3, prefer distinct final delivery stops to examine alternate
    geographic approaches at a bounded number of paid leg requests.
    """
    n = len(stops)
    if not 3 <= n <= 7:
        raise ValueError('Candidate comparison supports one to five delivery outlets.')
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_TRAFFIC_CANDIDATES:
        raise ValueError('Candidate count must be between one and four.')
    for node, periods in (windows or {}).items():
        if not periods:
            raise OptimisationError(
                f'{stops[node].get("name", "Outlet")} is closed on the selected delivery date.')
    try:
        ranking = ranked_orders(stops, matrix, departure, max_hours, windows)
    except RouteDataError as exc:
        raise OptimisationError(str(exc)) from exc
    if not ranking:
        raise OptimisationError('No feasible route under the initial driving-time estimates.')

    if count <= 2:
        return [item['order'] for item in ranking[:count]]

    selected = []
    used = set()
    seen_last = set()
    # One feasible order for each different last outlet, ranked by its
    # best snapshot score. This deliberately brings geographically diverse
    # alternatives into traffic validation without increasing the chosen cap.
    for item in ranking:
        last = item['order'][-2]
        if last not in seen_last:
            selected.append(item['order'])
            used.add(item['order'])
            seen_last.add(last)
            if len(selected) >= count:
                return selected
    for item in ranking:
        if item['order'] not in used:
            selected.append(item['order'])
            if len(selected) >= count:
                break
    return selected


def compare_traffic_candidates(stops, matrix, delivery_date, departure, windows,
                               get_leg_minutes, max_hours=12, max_requests=12,
                               candidate_count=2):
    """Compare up to four snapshot-feasible orders with leg-specific traffic.

    A request-budget exhaustion may leave earlier candidates feasible; these
    remain valid rather than being discarded. Missing/error responses are never
    imputed. Return metrics are rounded only for presentation.
    """
    if isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests < 0:
        raise ValueError('Maximum traffic request budget must be a nonnegative integer.')
    orders = candidate_orders(stops, matrix, departure, windows, max_hours,
                              count=candidate_count)
    tz = ZoneInfo('Europe/Malta')
    initial = datetime.combine(delivery_date,
                               datetime.strptime(departure, '%H:%M').time(), tzinfo=tz)
    cache = {}
    attempts = 0
    best = None
    errors = []
    comparisons = []

    for order in orders:
        current = initial
        route = [{**stops[0], 'arrival': departure, 'departure': departure,
                  'arrival_exact': initial.strftime('%H:%M:%S'),
                  'departure_exact': initial.strftime('%H:%M:%S'),
                  'drive_minutes': 0, 'wait_minutes': 0, 'service_minutes': 0}]
        driving = Decimal(0)
        feasible = True
        for prev, node in zip(order, order[1:]):
            lookup = (prev, node, current.isoformat())
            if lookup not in cache:
                if attempts >= max_requests:
                    errors.append('Traffic comparison reached the Google request limit.')
                    feasible = False
                    break
                # Exceptions propagate. The caller's journal has already
                # reserved this exact request and will retain any failure.
                duration = minute_value(get_leg_minutes(stops[prev], stops[node], current))
                cache[lookup] = duration
                attempts += 1
            drive = cache[lookup]
            driving += drive
            current += timedelta(microseconds=as_microseconds(drive))
            wait = Decimal(0)
            service = Decimal(0) if node == order[-1] else minute_value(
                stops[node].get('service_minutes')
                if stops[node].get('service_minutes') is not None else 5)

            if node in (windows or {}):
                options = []
                for opening, closing in windows[node]:
                    day_start = datetime.combine(delivery_date, datetime.min.time(), tzinfo=tz)
                    earliest = day_start + timedelta(minutes=float(opening))
                    latest = day_start + timedelta(minutes=float(minute_value(closing) - service))
                    candidate = max(current, earliest)
                    if candidate <= latest:
                        options.append(candidate)
                if not options:
                    errors.append(f'{stops[node].get("name", "Outlet")}: outside opening hours.')
                    feasible = False
                    break
                desired = min(options)
                wait = Decimal(str((desired - current).total_seconds())) / 60
                current = desired
            arrival = current
            current += timedelta(microseconds=as_microseconds(service))
            if (current - initial).total_seconds() > float(max_hours) * 3600 or current.date() != delivery_date:
                errors.append('Candidate exceeds the route duration or crosses midnight.')
                feasible = False
                break
            route.append({
                **stops[node],
                'arrival': arrival.strftime('%H:%M'),
                'departure': current.strftime('%H:%M'),
                'arrival_exact': arrival.strftime('%H:%M:%S'),
                'departure_exact': current.strftime('%H:%M:%S'),
                'drive_minutes': display_minutes(drive),
                'wait_minutes': display_minutes(wait),
                'service_minutes': display_minutes(service),
            })

        duration = Decimal(str((current - initial).total_seconds())) / 60
        comparisons.append({
            'order': [stops[node].get('name', str(node)) for node in order],
            'feasible': feasible,
            'return_time': current.strftime('%H:%M') if feasible else None,
            'duration_minutes': display_minutes(duration) if feasible else None,
            'driving_minutes': display_minutes(driving) if feasible else None,
            'last_outlet': stops[order[-2]].get('name', str(order[-2])),
        })
        if feasible:
            score = (current, driving, order)
            if best is None or score < best[0]:
                best = (score, route)

    if best is None:
        raise OptimisationError(
            'No candidate route passed traffic and opening-hours validation. '
            + ' '.join(errors))
    winning_order = best[0][2]
    winner_index = next(i for i, item in enumerate(comparisons)
                        if item['feasible'] and item['order'] ==
                        [stops[node].get('name', str(node)) for node in winning_order])
    baseline = comparisons[0]
    winner = comparisons[winner_index]
    saved = (round(baseline['duration_minutes'] - winner['duration_minutes'], 3)
             if baseline['feasible'] else None)
    return best[1], {
        'candidates_checked': len(orders),
        'candidate_limit': candidate_count,
        'google_leg_requests': attempts,
        'candidate_errors': errors,
        'comparisons': comparisons,
        'winner_index': winner_index,
        'minutes_saved_vs_first': saved,
        'global_time_dependent_optimum': False,
        'selection': ('top-two snapshot orders' if candidate_count <= 2
                      else 'diversified best snapshot route by last outlet'),
    }


def n_last(order):
    """Compatibility helper retained for older imports."""
    return order[-1]
