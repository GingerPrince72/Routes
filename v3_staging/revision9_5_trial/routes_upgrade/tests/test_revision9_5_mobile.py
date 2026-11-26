"""Revision 9.5: zero-network Google Maps links, place ID persistence."""
import json
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import database
import google_maps


def _params(url):
    assert url.startswith('https://www.google.com/maps/dir/?')
    return parse_qs(urlparse(url).query)


def test_export_uses_pharmacy_names_not_dropped_coordinate_pins(monkeypatch):
    monkeypatch.setattr(google_maps, '_post', lambda *a, **kw: 1/0)
    stops = [
        {'name': 'TXP', 'address': 'Ta Xbiex, Malta', 'latitude': 35.9, 'longitude': 14.49},
        {'name': 'Safi Pharmacy', 'address': 'Safi, Malta', 'latitude': 35.83, 'longitude': 14.48},
        {'name': 'Pasteur Pharmacy', 'address': 'Triq it-Tellerit, Safi, Malta'},
        {'name': 'Salus Pharmacy', 'address': 'Zurrieq, Malta'},
        {'name': 'Krypton Pharmacy', 'address': 'Swieqi, Malta'},
        {'name': 'TXP', 'address': 'Ta Xbiex, Malta', 'latitude': 35.9, 'longitude': 14.49},
    ]
    args = _params(google_maps.navigation_url(stops))
    assert args['api'] == ['1']
    assert args['travelmode'] == ['driving']
    assert args['origin'] == ['TXP, Ta Xbiex, Malta']
    assert args['destination'] == ['TXP, Ta Xbiex, Malta']
    assert args['waypoints'][0].split('|') == [
        'Safi Pharmacy, Safi, Malta',
        'Pasteur Pharmacy, Triq it-Tellerit, Safi, Malta',
        'Salus Pharmacy, Zurrieq, Malta',
        'Krypton Pharmacy, Swieqi, Malta',
    ]
    # No provider contact was needed to build the link.


def test_iphone_single_destination_uses_name_and_phone_location():
    params = _params(google_maps.navigate_to_stop_url({
        'name': 'Krypton Pharmacy', 'address': 'Triq Ta’ L-Ibraġ, Swieqi, Malta',
    }))
    assert 'origin' not in params
    assert 'waypoints' not in params
    assert params['destination'] == ['Krypton Pharmacy, Triq Ta’ L-Ibraġ, Swieqi, Malta']


def test_place_ids_used_for_precise_directories_without_guessing():
    stops = [
        {'name': 'Depot', 'place_id': 'depot-ID'},
        {'name': 'Shop A', 'place_id': 'shop-A'},
        {'name': 'Shop B', 'place_id': 'shop-B'},
        {'name': 'Depot', 'place_id': 'depot-ID'},
    ]
    args = _params(google_maps.navigation_url(stops))
    assert args['origin_place_id'] == ['depot-ID']
    assert args['destination_place_id'] == ['depot-ID']
    assert args['waypoint_place_ids'] == ['shop-A|shop-B']
    stops[1].pop('place_id')
    args = _params(google_maps.navigation_url(stops))
    assert 'waypoint_place_ids' not in args
    single = _params(google_maps.navigate_to_stop_url(stops[2]))
    assert single['destination_place_id'] == ['shop-B']


def test_legacy_coordinates_only_have_explicit_fallback():
    url = google_maps.navigate_to_stop_url({
        'latitude': 35.9, 'longitude': 14.49
    })
    assert _params(url)['destination'] == ['35.9000000,14.4900000']


def test_google_place_search_preserves_provider_place_id(monkeypatch):
    monkeypatch.setattr(google_maps, '_post', lambda *args, **kwargs: {
        'places': [{
            'id': 'ChIJ-PLACE-ID',
            'displayName': {'text': 'Safi Pharmacy'},
            'formattedAddress': 'Safi, Malta',
            'location': {'latitude': 35.83, 'longitude': 14.48},
        }]
    })
    result = google_maps.search_place('Safi Pharmacy')
    assert result[0]['place_id'] == 'ChIJ-PLACE-ID'


def test_old_sqlite_migrates_place_id_and_saves_it(tmp_path):
    old_path = database.DB_PATH
    database.DB_PATH = tmp_path / 'routes95.db'
    try:
        with sqlite3.connect(database.DB_PATH) as db:
            db.execute('CREATE TABLE locations (id INTEGER PRIMARY KEY, '
                       'name TEXT NOT NULL, latitude REAL, longitude REAL, '
                       'open_time TEXT, close_time TEXT, is_depot INTEGER DEFAULT 0)')
            db.execute("INSERT INTO locations (name,latitude,longitude) VALUES ('Safi',35.83,14.48)")
        database.init_db()
        assert database.all_locations()[0]['place_id'] == ''
        saved = database.all_locations()[0]
        saved['place_id'] = 'ChIJ-PLACE-ID'
        database.upsert_location(saved, saved['id'])
        assert database.all_locations()[0]['place_id'] == 'ChIJ-PLACE-ID'
    finally:
        database.DB_PATH = old_path
