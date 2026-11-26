"""Offline request journal and budget regression tests for Revision 9.3.

No Google API calls are made; all SQLite files live under pytest's tmp_path.
"""

from datetime import datetime, timedelta, timezone
import sqlite3
import threading

import pytest

from calculation_journal import CalculationJournal, GuardBlocked, RequestBudgetExceeded
from request_guard import (
    calculation_signature, estimate_matrix_elements, estimate_requests,
    is_duplicate, MAX_MATRIX_ELEMENTS,
)


def signature(ids=(1, 2), hours=12):
    return calculation_signature(ids, '2026-11-13', '08:00', hours, True, True)


def journal(tmp_path):
    return CalculationJournal(tmp_path / 'request_journal.db')


def budget():
    return estimate_requests(4, True, True)


def test_existing_budget_and_precise_signature():
    assert estimate_requests(4, True, True) == {
        'matrix_requests': 1, 'leg_requests': 10, 'total_requests': 11,
    }
    assert estimate_matrix_elements(4) == 25
    assert estimate_matrix_elements(24) == MAX_MATRIX_ELEMENTS
    assert signature(hours=8.5) != signature(hours=8.0)
    assert signature(ids=(1, 2)) != signature(ids=(2, 1))
    assert not is_duplicate(None, None, False)


def test_invalid_outlet_count_rejected():
    for bad in (-1, 1.5, True):
        with pytest.raises(ValueError):
            estimate_requests(bad, True, True)


def test_duplicate_lock_persists_across_instances(tmp_path):
    path = tmp_path / 'journal.db'
    one = CalculationJournal(path)
    attempt = one.begin(signature(), budget())
    two = CalculationJournal(path)
    with pytest.raises(GuardBlocked):
        two.begin(signature(), budget(), allow_repeat=True)
    one.succeed(attempt, {'route': [{'name': 'TXP'}], 'traffic': True})
    with pytest.raises(GuardBlocked):
        two.begin(signature(), budget())
    next_attempt = two.begin(signature(), budget(), allow_repeat=True)
    assert next_attempt != attempt


def test_failed_request_is_charged_against_local_counter(tmp_path):
    guard = journal(tmp_path)
    attempt = guard.begin(signature(), budget())
    guard.note_request(attempt, 'matrix')
    guard.note_request(attempt, 'leg')
    guard.fail(attempt, 'MapsError')
    row = guard.latest(signature())
    assert row['state'] == 'failed'
    assert (row['matrix_count'], row['leg_count']) == (1, 1)
    with pytest.raises(GuardBlocked):
        guard.begin(signature(), budget())
    guard.begin(signature(), budget(), allow_repeat=True)


def test_request_budget_checked_before_call(tmp_path):
    guard = journal(tmp_path)
    attempt = guard.begin(signature(), budget())
    guard.note_request(attempt, 'matrix')
    with pytest.raises(RequestBudgetExceeded):
        guard.note_request(attempt, 'matrix')
    for _ in range(10):
        guard.note_request(attempt, 'leg')
    with pytest.raises(RequestBudgetExceeded):
        guard.note_request(attempt, 'leg')
    assert guard.latest(signature())['leg_count'] == 10


def test_finished_attempt_cannot_request_again(tmp_path):
    guard = journal(tmp_path)
    attempt = guard.begin(signature(), budget())
    guard.fail(attempt, 'MapsError')
    with pytest.raises(GuardBlocked):
        guard.note_request(attempt, 'matrix')


def test_saved_result_survives_browser_refresh(tmp_path):
    path = tmp_path / 'journal.db'
    guard = CalculationJournal(path)
    attempt = guard.begin(signature(), budget())
    payload = {'route': [{'name': 'TXP'}, {'name': 'Safi Pharmacy'}],
               'unverified_names': ['Safi Pharmacy'], 'comparison_info': None}
    guard.succeed(attempt, payload)
    reloaded = CalculationJournal(path).last_success(signature())
    assert reloaded['payload'] == payload


def test_old_success_survives_failed_authorised_repeat(tmp_path):
    guard = journal(tmp_path)
    original = guard.begin(signature(), budget())
    payload = {'route': [{'name': 'TXP'}]}
    guard.succeed(original, payload)
    retry = guard.begin(signature(), budget(), allow_repeat=True)
    guard.note_request(retry, 'matrix')
    guard.fail(retry, 'Request failed')
    assert guard.latest(signature())['state'] == 'failed'
    assert guard.last_success(signature())['payload'] == payload


def test_unrelated_signatures_have_independent_history(tmp_path):
    guard = journal(tmp_path)
    attempt = guard.begin(signature((1, 2)), budget())
    guard.succeed(attempt, {'route': []})
    assert guard.latest(signature((2, 1))) is None
    assert guard.begin(signature((2, 1)), budget()) != attempt


def test_abandoned_attempt_recovery_requires_age(tmp_path):
    guard = journal(tmp_path)
    attempt = guard.begin(signature(), budget())
    with pytest.raises(GuardBlocked):
        guard.interrupt_if_stale(attempt)
    old = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
    with sqlite3.connect(guard.path) as db:
        db.execute('UPDATE attempts SET started_utc=? WHERE id=?', (old, attempt))
    assert guard.is_stale(guard.latest(signature()))
    guard.interrupt_if_stale(attempt)
    assert guard.latest(signature())['state'] == 'interrupted'
    with pytest.raises(GuardBlocked):
        guard.begin(signature(), budget())
    guard.begin(signature(), budget(), allow_repeat=True)


def test_concurrent_identical_calculation_blocked(tmp_path):
    path = tmp_path / 'journal.db'
    guard = CalculationJournal(path)
    barrier = threading.Barrier(2)
    results = []
    lock = threading.Lock()

    def worker():
        barrier.wait(timeout=5)
        try:
            guard.begin(signature(), budget())
            output = 'started'
        except GuardBlocked:
            output = 'blocked'
        with lock:
            results.append(output)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert sorted(results) == ['blocked', 'started']
