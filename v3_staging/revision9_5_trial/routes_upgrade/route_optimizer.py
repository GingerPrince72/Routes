"""Delivery-route optimiser. Decimal minutes for exact small routes;
integer seconds at the OR-Tools solver boundary for larger routes.

Routes are scored by earliest feasible return to TXP (including waiting), then
by driving duration. Google durations are never rounded to integer minutes
before route selection.
"""

from datetime import datetime, timedelta
from decimal import Decimal
import json
from zoneinfo import ZoneInfo

from route_analysis import (
    RouteDataError, as_ceil_seconds, as_microseconds, clock_text,
    display_minutes, hhmm_minutes, minute_value, ranked_orders,
)


class OptimisationError(RuntimeError):
    pass


def minutes(value):
    """Preserve the public HH:MM conversion used by earlier releases."""
    return hhmm_minutes(value)


def windows_for_date(stops, day):
    """Unknown/unverified hours stay unrestricted; explicit CLOSED is infeasible."""
    dayname = day.strftime('%A')
    result = {}
    for i, stop in enumerate(stops):
        if i in (0, len(stops) - 1):
            continue
        raw = stop.get('weekly_hours') or '{}'
        try:
            schedule = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            schedule = {}
        if not isinstance(schedule, dict):
            schedule = {}
        periods = schedule.get(dayname, [])
        if periods == 'CLOSED':
            result[i] = []
        elif periods:
            result[i] = [(minutes(opening), minutes(closing))
                         for opening, closing in periods]
    return result


def _exact_small_route(stops, matrix, departure, max_hours, windows):
    """Exhaustively evaluate up to seven outlets at sub-minute precision."""
    for index, periods in (windows or {}).items():
        if not periods:
            raise OptimisationError(
                f'{stops[index].get("name", "Outlet")} is closed on the selected delivery date.')
    try:
        ranked = ranked_orders(stops, matrix, departure, max_hours, windows)
    except (ValueError, RouteDataError) as exc:
        raise OptimisationError(str(exc)) from exc
    if not ranked:
        raise OptimisationError(
            'No feasible route. Check opening hours, travel times, '
            'service durations and departure time.')
    winner = ranked[0]
    route = []
    for step in winner['steps']:
        stop = stops[step['index']]
        route.append({
            **stop,
            'arrival': clock_text(step['arrival']),
            'departure': clock_text(step['departure']),
            'arrival_exact': clock_text(step['arrival'], seconds=True),
            'departure_exact': clock_text(step['departure'], seconds=True),
            'drive_minutes': display_minutes(step['drive']),
            'wait_minutes': display_minutes(step['wait']),
            'service_minutes': display_minutes(step['service']),
        })
    return route


def _ortools_large_route(stops, matrix, departure, max_hours, windows, solve_seconds):
    """For 8+ outlets use OR-Tools with second-level arc costs and time windows."""
    try:
        from ortools.constraint_solver import pywrapcp, routing_enums_pb2
    except ImportError as exc:
        raise OptimisationError('OR-Tools is required for routes with eight or more outlets.') from exc

    n = len(stops)
    manager = pywrapcp.RoutingIndexManager(n, 1, [0], [n - 1])
    routing = pywrapcp.RoutingModel(manager)
    services = [0] + [
        int(minute_value(stop.get('service_minutes')
                         if stop.get('service_minutes') is not None else 5)) * 60
        for stop in stops[1:-1]
    ] + [0]

    def transit(from_idx, to_idx):
        a = manager.IndexToNode(from_idx)
        b = manager.IndexToNode(to_idx)
        return as_ceil_seconds(matrix[a][b]) + services[a]

    callback = routing.RegisterTransitCallback(transit)
    routing.SetArcCostEvaluatorOfAllVehicles(callback)
    routing.AddDimension(callback, 24 * 3600, 24 * 3600 - 1, False, 'Time')
    dim = routing.GetDimensionOrDie('Time')
    dim.SetGlobalSpanCostCoefficient(100)
    start = minutes(departure) * 60
    dim.CumulVar(routing.Start(0)).SetRange(start, start)

    for node, periods in (windows or {}).items():
        if not periods:
            raise OptimisationError(
                f'{stops[node].get("name", "Outlet")} is closed on the selected delivery date.')
        allowed = []
        for opening, closing in periods:
            low = int(minute_value(opening)) * 60
            high = int(minute_value(closing)) * 60 - services[node]
            if high >= low:
                allowed.append((low, high))
        if not allowed:
            raise OptimisationError(
                f'{stops[node].get("name", "Outlet")} has no '
                'opening interval long enough for unloading.')
        allowed.sort()
        var = dim.CumulVar(manager.NodeToIndex(node))
        var.SetRange(allowed[0][0], max(end for _, end in allowed))
        boundary = allowed[0][1]
        for low, high in allowed[1:]:
            if low > boundary + 1:
                var.RemoveInterval(boundary + 1, low - 1)
            boundary = max(boundary, high)

    end_limit = min(24 * 3600 - 1, start + int(minute_value(max_hours) * 3600))
    dim.CumulVar(routing.End(0)).SetRange(start, end_limit)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC)
    params.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH)
    params.time_limit.seconds = max(1, int(solve_seconds))
    solution = routing.SolveWithParameters(params)
    if not solution:
        raise OptimisationError(
            'No feasible route. Check opening hours, travel times, '
            'service durations and departure time.')

    route = []
    idx = routing.Start(0)
    previous_node = None
    while True:
        node = manager.IndexToNode(idx)
        arrival_seconds = solution.Value(dim.CumulVar(idx))
        departure_seconds = arrival_seconds + services[node]
        drive = (Decimal(0) if previous_node is None
                 else minute_value(matrix[previous_node][node]))
        route.append({
            **stops[node],
            'arrival': clock_text(Decimal(arrival_seconds) / 60),
            'departure': clock_text(Decimal(departure_seconds) / 60),
            'arrival_exact': clock_text(Decimal(arrival_seconds) / 60, seconds=True),
            'departure_exact': clock_text(Decimal(departure_seconds) / 60, seconds=True),
            'drive_minutes': display_minutes(drive),
            'wait_minutes': None,  # OR-Tools slack values may not be bound in all builds.
            'service_minutes': services[node] / 60,
        })
        previous_node = node
        if routing.IsEnd(idx):
            break
        idx = solution.Value(routing.NextVar(idx))
    return route


def optimise(stops, matrix, departure='08:00', max_hours=12,
             windows=None, solve_seconds=10):
    if len(stops) < 3:
        raise OptimisationError('TXP and at least one delivery outlet are required.')
    if stops[0].get('id') != 'TXP' or stops[-1].get('id') != 'TXP':
        raise OptimisationError('TXP must be the first and last route location.')
    n = len(stops)
    if len(matrix) != n or any(len(row) != n for row in matrix):
        raise OptimisationError('Invalid travel-time matrix.')
    try:
        if n - 2 <= 7:
            return _exact_small_route(stops, matrix, departure, max_hours, windows)
        return _ortools_large_route(stops, matrix, departure, max_hours,
                                   windows, solve_seconds)
    except RouteDataError as exc:
        raise OptimisationError(str(exc)) from exc


def validate_traffic_route(route, delivery_date, departure, windows,
                           get_leg_minutes, max_hours=12):
    """Check one selected order at actual leg departures, retaining seconds.

    Does not claim global time-dependent optimality; call budget must be enforced
    by the caller's persistent journal before each get_leg_minutes call.
    """
    tz = ZoneInfo('Europe/Malta')
    current = datetime.combine(delivery_date,
                               datetime.strptime(departure, '%H:%M').time(), tzinfo=tz)
    started = current
    result = []
    for index, stop in enumerate(route):
        if index == 0:
            drive = Decimal(0)
        else:
            drive = minute_value(get_leg_minutes(route[index - 1], stop, current))
            current += timedelta(microseconds=as_microseconds(drive))
        service = Decimal(0) if index in (0, len(route) - 1) else minute_value(
            stop.get('service_minutes') or 0)
        waiting = Decimal(0)
        if index not in (0, len(route) - 1) and index in (windows or {}):
            options = []
            for opening, closing in windows[index]:
                start = datetime.combine(delivery_date, datetime.min.time(), tzinfo=tz)
                earliest = start + timedelta(minutes=float(opening))
                latest = start + timedelta(minutes=float(closing - service))
                candidate = max(current, earliest)
                if candidate <= latest:
                    options.append(candidate)
            if not options:
                raise OptimisationError(
                    f'Traffic-aware route reaches {stop.get("name", "outlet")} '
                    'outside its opening hours. Try another departure or traffic-unaware mode.')
            desired = min(options)
            waiting = Decimal(str((desired - current).total_seconds())) / 60
            current = desired
        arrival = current
        current += timedelta(microseconds=as_microseconds(service))
        result.append({
            **stop,
            'arrival': arrival.strftime('%H:%M'),
            'departure': current.strftime('%H:%M'),
            'arrival_exact': arrival.strftime('%H:%M:%S'),
            'departure_exact': current.strftime('%H:%M:%S'),
            'drive_minutes': display_minutes(drive),
            'wait_minutes': display_minutes(waiting),
            'service_minutes': display_minutes(service),
        })
    if (current - started).total_seconds() > float(max_hours) * 3600 or current.date() != delivery_date:
        raise OptimisationError('Traffic-aware route exceeds the maximum route duration or crosses midnight.')
    return result
