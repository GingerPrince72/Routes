"""Run the full Streamlit route page with in-memory dependencies, no network.

This verifies that the actual UI calculation flow uses the durable guard. All
provider and database functions are replaced with test doubles. No live API key.
"""

from datetime import date
from pathlib import Path
from types import ModuleType, SimpleNamespace
import json
import runpy
import sys

from calculation_journal import CalculationJournal


APP_FILE = Path(__file__).resolve().parents[1] / 'app.py'


class SessionState(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name, value):
        self[name] = value


class Region:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class FakeUI(ModuleType):
    def __init__(self):
        super().__init__('streamlit')
        self.session_state = SessionState()
        self.sidebar = SimpleNamespace(radio=lambda *args, **kwargs: 'Route Planner')
        self.click_calculate = True
        self.departure = '08:00'
        self.tables_rendered = 0
        self.consent_repeat = False
        self.available_modes = []

    def __getattr__(self, name):
        return lambda *args, **kwargs: None

    def columns(self, widths, *, gap=None, **kwargs):
        return [Region() for _ in range(len(widths) if isinstance(widths, list) else widths)]

    def expander(self, *args, **kwargs):
        return Region()

    def spinner(self, *args, **kwargs):
        return Region()

    def text_input(self, label, *args, **kwargs):
        if label == 'Departure time (HH:MM)':
            return self.departure
        return kwargs.get('value', '')

    def number_input(self, *args, **kwargs):
        return kwargs.get('value', args[3] if len(args) >= 4 else 5)

    def selectbox(self, label, options, *args, **kwargs):
        if label == 'Driving-time mode':
            self.available_modes = list(options)
        return list(options)[kwargs.get('index', 0)]

    def date_input(self, *args, **kwargs):
        return date(2026, 11, 13)

    def checkbox(self, label, *args, **kwargs):
        if label.startswith('I authorise another calculation'):
            return self.consent_repeat and not kwargs.get('disabled', False)
        return False

    def button(self, label, *args, **kwargs):
        if label == 'Calculate optimised route':
            return self.click_calculate and not kwargs.get('disabled', False)
        return False

    def dataframe(self, *args, **kwargs):
        self.tables_rendered += 1


def test_app_calculates_once_and_restores_result_after_refresh(monkeypatch, tmp_path):
    fake_ui = FakeUI()
    calls = []
    locations = [
        {'id': i, 'name': f'Pharmacy {i}', 'address': 'Malta',
         'latitude': 35.8 + i * .01, 'longitude': 14.4 + i * .01,
         'service_minutes': 5, 'weekly_hours': '{}'}
        for i in range(1, 5)
    ]
    settings = {
        'active_route_ids': json.dumps([1, 2, 3, 4]),
        'active_route_name': 'New delivery route',
        'depot_json': json.dumps({
            'name': 'TXP', 'address': 'Ta Xbiex Malta',
            'latitude': 35.90, 'longitude': 14.49,
        }),
    }
    db = ModuleType('database')
    db.DB_PATH = tmp_path / 'routes.db'
    db.init_db = lambda: None
    db.all_locations = lambda: locations
    db.get_setting = lambda name: settings.get(name)
    db.save_setting = lambda name, value: settings.__setitem__(name, value)
    db.delete_location = lambda *args: None
    db.upsert_location = lambda *args: None

    gmaps = ModuleType('google_maps')
    gmaps.MapsError = type('MapsError', (RuntimeError,), {})

    def fake_route_matrix(stops, traffic=False, departure_at=None):
        calls.append('matrix')
        return [[0 if i == j else 5 for j in range(len(stops))]
                for i in range(len(stops))]

    gmaps.route_matrix = fake_route_matrix
    gmaps.traffic_leg_minutes = lambda *args: 5
    gmaps.navigation_url = lambda result: 'https://www.google.com/maps/dir/'
    gmaps.navigate_to_stop_url = lambda stop: 'https://www.google.com/maps/dir/?api=1'
    gmaps.search_place = lambda *args: []

    opt = ModuleType('route_optimizer')
    opt.OptimisationError = type('OptimisationError', (RuntimeError,), {})
    opt.minutes = lambda clock: int(clock[:2]) * 60 + int(clock[3:5])
    opt.windows_for_date = lambda *args: {}
    opt.validate_traffic_route = lambda *args: None

    def fake_optimise(stops, matrix, departure, max_hours, windows):
        return [{**stop, 'arrival': '08:00', 'departure': '08:00',
                 'drive_minutes': 5, 'wait_minutes': 0, 'service_minutes': 5}
                for stop in stops]

    opt.optimise = fake_optimise
    traffic_candidates = ModuleType('traffic_candidates')
    traffic_candidates.compare_traffic_candidates = lambda *args, **kwargs: None

    monkeypatch.setenv('GOOGLE_MAPS_API_KEY', 'TEST_ONLY_OFFLINE_NO_NETWORK')
    monkeypatch.setenv('ROUTES_REQUEST_JOURNAL_PATH', str(tmp_path / 'attempts.db'))
    for name, module in [('streamlit', fake_ui), ('database', db),
                         ('google_maps', gmaps), ('route_optimizer', opt),
                         ('traffic_candidates', traffic_candidates)]:
        monkeypatch.setitem(sys.modules, name, module)

    # First run: one fake request; result is saved outside routes.db.
    runpy.run_path(str(APP_FILE), run_name='__main__')
    assert calls == ['matrix']
    assert 'Compare 2–4 traffic-aware orders' in fake_ui.available_modes
    assert fake_ui.tables_rendered == 1
    assert CalculationJournal(tmp_path / 'attempts.db').latest(
        next(iter(_read_signatures(tmp_path / 'attempts.db'))))['state'] == 'succeeded'

    # Second run: the button is disabled for the exact same signature, and the
    # successful route is rendered from disk without another provider request.
    fake_ui.tables_rendered = 0
    runpy.run_path(str(APP_FILE), run_name='__main__')
    assert calls == ['matrix']
    assert fake_ui.tables_rendered == 1

    # Changed inputs are a new signature; the old result is NOT displayed.
    fake_ui.departure = '08:15'
    fake_ui.click_calculate = False
    fake_ui.tables_rendered = 0
    runpy.run_path(str(APP_FILE), run_name='__main__')
    assert fake_ui.tables_rendered == 0
    assert calls == ['matrix']

    # Simulate Google failing after the journal recorded the attempt.
    fake_ui.departure = '08:30'
    fake_ui.click_calculate = True
    original_matrix = gmaps.route_matrix

    def failing_matrix(*args, **kwargs):
        calls.append('failed-matrix')
        raise gmaps.MapsError('Simulated provider failure; no network')

    gmaps.route_matrix = failing_matrix
    runpy.run_path(str(APP_FILE), run_name='__main__')
    assert calls == ['matrix', 'failed-matrix']
    assert CalculationJournal(tmp_path / 'attempts.db').latest(
        list(_read_signatures(tmp_path / 'attempts.db'))[-1])['state'] == 'failed'

    # Retrying identical inputs without explicit consent stays blocked.
    runpy.run_path(str(APP_FILE), run_name='__main__')
    assert calls == ['matrix', 'failed-matrix']

    # Explicitly authorising a retry permits exactly one more calculation.
    gmaps.route_matrix = original_matrix
    fake_ui.consent_repeat = True
    runpy.run_path(str(APP_FILE), run_name='__main__')
    assert calls == ['matrix', 'failed-matrix', 'matrix']


def _read_signatures(path):
    import sqlite3
    with sqlite3.connect(path) as db:
        return [row[0] for row in db.execute('SELECT signature FROM attempts ORDER BY id')]
