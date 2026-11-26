from request_guard import calculation_signature, estimate_requests, is_duplicate


def test_budget_without_traffic():
    assert estimate_requests(4, False, False) == {'matrix_requests': 1, 'leg_requests': 0, 'total_requests': 1}


def test_budget_traffic_single():
    assert estimate_requests(4, True, False)['total_requests'] == 6


def test_budget_comparison_and_limit():
    assert estimate_requests(4, True, True)['total_requests'] == 11
    assert estimate_requests(6, True, True)['total_requests'] == 8


def test_signature_changes_with_route_settings():
    base = calculation_signature([1,2], '2026-11-13', '08:00', 12, True, True)
    assert base == calculation_signature([1,2], '2026-11-13', '08:00', 12, True, True)
    assert base != calculation_signature([2,1], '2026-11-13', '08:00', 12, True, True)
    assert base != calculation_signature([1,2], '2026-11-13', '09:00', 12, True, True)


def test_repeat_requires_override():
    assert is_duplicate('same','same',False)
    assert not is_duplicate('same','same',True)
    assert not is_duplicate('changed','same',False)
