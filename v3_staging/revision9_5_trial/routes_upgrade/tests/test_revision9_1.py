from datetime import date
from test_revision9 import fixtures
from traffic_candidates import compare_traffic_candidates

def test_comparison_reports_winner_and_savings():
    stops, matrix = fixtures()
    def fake(a, b, when):
        return 50 if a['id'] == 1 and b['id'] == 2 else 5
    route, stats = compare_traffic_candidates(
        stops, matrix, date(2026, 11, 13), '08:00', {}, fake)
    assert len(stats['comparisons']) == 2
    assert stats['winner_index'] == 1
    assert stats['comparisons'][0]['return_time'] == '09:10'
    assert stats['comparisons'][1]['return_time'] == '08:25'
    assert stats['minutes_saved_vs_first'] == 45

def test_report_handles_unvalidated_second_candidate():
    stops, matrix = fixtures()
    route, stats = compare_traffic_candidates(
        stops, matrix, date(2026, 11, 13), '08:00', {},
        lambda a,b,when: 5, max_requests=3)
    assert stats['winner_index'] == 0
    assert stats['comparisons'][1]['feasible'] is False
    assert stats['minutes_saved_vs_first'] == 0
