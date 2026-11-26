"""Optional 8-outlet OR-Tools smoke test; runs where ortools is installed.

The build environment for the audit ZIP may lack OR-Tools. Codespaces has
it according to requirements; this checks the second-granularity branch.
No Google requests or changes to user data.
"""
from decimal import Decimal

import pytest

from route_optimizer import optimise


def test_8_outlet_solver_supports_seconds_and_split_windows():
    pytest.importorskip('ortools')
    stops = [{'id': 'TXP', 'name': 'TXP', 'service_minutes': 0}]
    stops += [{'id': i, 'name': f'Stop {i}', 'service_minutes': 1}
              for i in range(1, 9)]
    stops.append(dict(stops[0]))
    n = len(stops)
    matrix = [[Decimal(0) if i == j else Decimal('0.25')
               for j in range(n)] for i in range(n)]
    windows = {3: [(540, 550), (600, 620)]}
    route = optimise(stops, matrix, departure='08:00', max_hours=12,
                     windows=windows, solve_seconds=2)
    assert len(route) == n
    assert route[0]['id'] == route[-1]['id'] == 'TXP'
    assert sorted(s['id'] for s in route[1:-1]) == list(range(1, 9))
    assert all(s.get('arrival_exact', '').count(':') == 2 for s in route)
    assert all(s['drive_minutes'] == .25 for s in route[1:])
    windowed = next(s for s in route if s['id'] == 3)
    assert ('09:00:00' <= windowed['arrival_exact'] <= '09:09:00'
            or '10:00:00' <= windowed['arrival_exact'] <= '10:19:00')
