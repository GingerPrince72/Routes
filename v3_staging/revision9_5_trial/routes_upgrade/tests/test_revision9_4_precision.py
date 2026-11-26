"""Revision 9.4: full-duration precision, exact snapshot comparison, budgets.

No network or real outlets databases. All Google providers are monkeypatched.
"""
from datetime import date
from decimal import Decimal
from itertools import permutations
import json

import pytest

import google_maps
from route_analysis import (RouteDataError, as_ceil_seconds, as_microseconds,
                            clock_text, evaluate_order, minute_value,
                            ranked_orders, snapshot_report)
from route_optimizer import OptimisationError, optimise, windows_for_date
from traffic_candidates import candidate_orders, compare_traffic_candidates
from request_guard import calculation_signature, estimate_requests
from calculation_journal import CalculationJournal, GuardBlocked


def _stops():
    return [
        {'id': 'TXP', 'name': 'TXP', 'service_minutes': 0},
        {'id': 1, 'name': 'Krypton', 'service_minutes': 0},
        {'id': 2, 'name': 'Safi', 'service_minutes': 0},
        {'id': 'TXP', 'name': 'TXP', 'service_minutes': 0},
    ]


def test_google_decimal_duration_retains_seconds_and_subseconds():
    assert google_maps._minutes_from_duration('59s') == Decimal(59) / 60
    assert google_maps._minutes_from_duration('59.125s') * 60 == Decimal('59.125')
    assert google_maps._minutes_from_duration('0s') == 0
    assert google_maps._minutes_from_duration('120.000001s') * 60 == Decimal('120.000001')
    assert as_microseconds(google_maps._minutes_from_duration('59.125s')) == 59_125_000
    assert as_ceil_seconds(google_maps._minutes_from_duration('59.125s')) == 60
    assert clock_text(Decimal('481.4999')) == '08:01'
    assert clock_text(Decimal('481.5')) == '08:02'


@pytest.mark.parametrize('bad', ['nan', 'NaNs', '-4s', 'infinitys', '', '1', 'abc', None, 2])
def test_google_rejects_malformed_or_nonfinite_durations(bad):
    with pytest.raises(google_maps.MapsError):
        google_maps._minutes_from_duration(bad)


def test_provider_matrix_subminute_arcs_reach_optimiser(monkeypatch):
    data = [
        {'originIndex': i, 'destinationIndex': j,
         'condition': 'ROUTE_EXISTS', 'status': {},
         'duration': ('0s' if i == j else ('59.5s' if (i, j) == (0, 1) else '60s'))}
        for i in range(3) for j in range(3)
    ]
    calls = []

    def fake_post(url, payload, mask, timeout=45):
        calls.append(payload)
        return data

    monkeypatch.setattr(google_maps, '_post', fake_post)
    s = [{'name': f'P{i}', 'latitude': 35.8 + i * .01,
          'longitude': 14.5 + i * .01} for i in range(3)]
    s += [dict(s[0])]
    matrix = google_maps.route_matrix(s)
    assert len(calls) == 1
    assert matrix[0][1] == Decimal('59.5') / Decimal(60)
    assert matrix[0][3] == 0


def test_precision_changes_winner_where_rounded_minutes_tie():
    s = _stops()
    # Krypton-first 59+59+59=177s, Safi-first 60+60+56=176s.
    # Legacy ceil-to-minute scoring ties both at 3 min and chooses Krypton.
    matrix = [[0, Decimal(59)/60, Decimal(60)/60, 0],
              [Decimal(99)/60, 0, Decimal(59)/60, Decimal(56)/60],
              [Decimal(99)/60, Decimal(60)/60, 0, Decimal(59)/60],
              [0, 0, 0, 0]]
    route = optimise(s, matrix, '08:00', 12, {})
    assert [x['id'] for x in route] == ['TXP', 2, 1, 'TXP']
    assert route[-1]['arrival_exact'] == '08:02:56'
    assert route[1]['drive_minutes'] == 1
    ranking = ranked_orders(s, matrix, '08:00')
    assert abs(ranking[0]['duration_minutes'] - Decimal(176) / 60) < Decimal('1e-20')
    assert abs(ranking[1]['duration_minutes'] - Decimal(177) / 60) < Decimal('1e-20')


def test_split_windows_and_waits_use_precise_arrival():
    s = [
        {'id': 'TXP', 'name': 'Depot', 'service_minutes': 0},
        {'id': 1, 'name': 'Shop', 'service_minutes': 5},
        {'id': 'TXP', 'name': 'Depot', 'service_minutes': 0},
    ]
    matrix = [[0, Decimal(61)/60, 0], [0, 0, Decimal(59)/60], [0, 0, 0]]
    windows = {1: [(480, 481), (490, 500)]}
    route = optimise(s, matrix, '08:00', 12, windows)
    assert route[1]['arrival_exact'] == '08:10:00'
    assert route[1]['departure_exact'] == '08:15:00'
    assert route[1]['wait_minutes'] == pytest.approx(8.983, abs=.001)
    assert route[-1]['arrival_exact'] == '08:15:59'
    assert evaluate_order((0, 1, 2), s, matrix, '08:00', 12, {1: [(480, 481)]}) is None


def test_report_exhaustive_best_by_last_includes_krypton_last():
    s = [
        {'id': 'TXP', 'name': 'TXP'},
        *[{'id': i, 'name': name, 'service_minutes': 5}
          for i, name in enumerate(['Krypton', 'Pasteur', 'Salus', 'Safi'], start=1)],
        {'id': 'TXP', 'name': 'TXP'},
    ]
    n = len(s)
    matrix = [[Decimal(0) if i == j else Decimal('3.40') for j in range(n)]
              for i in range(n)]
    report = snapshot_report(s, matrix, '08:00', 12)
    assert report['evaluated_orders'] == 24
    assert len(report['top_routes']) == 5
    assert {item['last_outlet'] for item in report['best_by_last']} == {
        'Krypton', 'Pasteur', 'Salus', 'Safi'}
    assert report['raw_matrix_saved'] is False
    assert len(report['matrix_digest']) == 64
    assert 'matrix' not in report or report['matrix'] is None
    assert report['top_routes'][0]['duration_minutes'] == 37
    assert all(len(item['order']) == 6 for item in report['best_by_last'])


def test_snapshot_returns_no_fake_route_when_all_closed():
    s = _stops()
    matrix = [[0 if i == j else 5 for j in range(4)] for i in range(4)]
    report = snapshot_report(s, matrix, '08:00', 12, {1: []})
    assert report['evaluated_orders'] == 0
    assert not report['top_routes']


def test_traffic_candidates_diversify_by_last_stop_without_extra_calls():
    s = [
        {'id': 'TXP', 'name': 'TXP'},
        *[{'id': i, 'name': name, 'service_minutes': 5}
          for i, name in enumerate(['Krypton', 'Pasteur', 'Salus', 'Safi'], start=1)],
        {'id': 'TXP', 'name': 'TXP'},
    ]
    n = len(s)
    matrix = [[0 if i == j else 5 for j in range(n)] for i in range(n)]
    old_two = candidate_orders(s, matrix, '08:00', {}, count=2)
    assert old_two == [(0, 1, 2, 3, 4, 5), (0, 1, 2, 4, 3, 5)]
    alternatives = candidate_orders(s, matrix, '08:00', {}, count=4)
    assert len(alternatives) == 4
    assert {order[-2] for order in alternatives} == {1, 2, 3, 4}
    assert alternatives[0] == old_two[0]


def test_four_candidate_comparison_bounded_and_subminute(monkeypatch):
    s = [
        {'id': 'TXP', 'name': 'TXP'},
        *[{'id': i, 'name': name, 'service_minutes': 5}
          for i, name in enumerate(['Krypton', 'Pasteur', 'Salus', 'Safi'], start=1)],
        {'id': 'TXP', 'name': 'TXP'},
    ]
    n = len(s)
    matrix = [[0 if i == j else Decimal('2.5') for j in range(n)]
              for i in range(n)]
    calls = []

    def fake_leg(a, b, departure):
        calls.append(departure)
        return Decimal('2.375')

    route, report = compare_traffic_candidates(
        s, matrix, date(2026, 11, 13), '08:00', {}, fake_leg,
        max_requests=20, candidate_count=4)
    assert len(report['comparisons']) == 4
    assert {x['last_outlet'] for x in report['comparisons']} == {
        'Krypton', 'Pasteur', 'Salus', 'Safi'}
    assert report['google_leg_requests'] == len(calls)
    assert 0 < len(calls) <= 20
    assert any(when.second != 0 for when in calls)
    assert route[-1]['departure_exact'] == '08:31:52'
    assert report['global_time_dependent_optimum'] is False


def test_budget_increases_only_with_explicit_candidate_count():
    assert estimate_requests(4, False, False, candidate_count=4)['total_requests'] == 1
    assert estimate_requests(4, True, False, candidate_count=4)['total_requests'] == 6
    assert estimate_requests(4, True, True, candidate_count=2)['total_requests'] == 11
    assert estimate_requests(4, True, True, candidate_count=3)['total_requests'] == 16
    assert estimate_requests(4, True, True, candidate_count=4)['total_requests'] == 21
    with pytest.raises(ValueError):
        estimate_requests(4, True, True, candidate_count=5)


def test_signature_distinguishes_precision_and_spending_levels():
    base = ([1, 2], date(2026, 11, 13), '08:00', 12, True, True)
    signatures = [calculation_signature(*base, candidate_count=n) for n in (2, 3, 4)]
    assert len(set(signatures)) == 3
    assert calculation_signature(*base, candidate_count=2) != \
           calculation_signature(*base, candidate_count=4)
    assert calculation_signature(*base[:-2], False, False, candidate_count=2) == \
           calculation_signature(*base[:-2], False, False, candidate_count=4)


def test_94_isolated_journal_never_touches_93(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delenv('ROUTES_REQUEST_JOURNAL_PATH', raising=False)
    guard = CalculationJournal()
    assert guard.path.name == 'revision9_4_request_journal.db'
    signature = calculation_signature([1], '2026-11-13', '08:00', 12, False, False)
    budget = estimate_requests(1, False, False)
    attempt = guard.begin(signature, budget)
    guard.note_request(attempt, 'matrix')
    guard.succeed(attempt, {'route': []})
    with pytest.raises(GuardBlocked):
        CalculationJournal().begin(signature, budget)
    assert not (tmp_path / '.local/state/txp_routes/revision9_3_request_journal.db').exists()
