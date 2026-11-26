"""Offline regression tests for Revision 9.3 Google matrix batching."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import google_maps


def sample_stops():
    depot = {"name": "TXP", "latitude": 35.9005, "longitude": 14.4933}
    stops = [depot]
    for index in range(4):
        stops.append({
            "name": f"Pharmacy {index + 1}",
            "latitude": 35.81 + index * 0.01,
            "longitude": 14.41 + index * 0.01,
        })
    return stops + [dict(depot)]


def fake_response(payload):
    results = []
    for i, origin in enumerate(payload["origins"]):
        for j, destination in enumerate(payload["destinations"]):
            a = origin["waypoint"]["location"]["latLng"]
            b = destination["waypoint"]["location"]["latLng"]
            seconds = 0 if a == b else (i * 10 + j + 1) * 60
            results.append({
                "originIndex": i,
                "destinationIndex": j,
                "condition": "ROUTE_EXISTS",
                "status": {},
                "duration": f"{seconds}s",
            })
    return list(reversed(results))


def test_single_batched_request_and_depot_mapping(monkeypatch):
    calls = []

    def fake_post(url, payload, mask, timeout=45):
        calls.append((url, payload, mask))
        return fake_response(payload)

    monkeypatch.setattr(google_maps, "_post", fake_post)
    matrix = google_maps.route_matrix(sample_stops())

    assert len(calls) == 1
    payload = calls[0][1]
    assert len(payload["origins"]) == 5
    assert len(payload["destinations"]) == 5
    assert "status" in calls[0][2]
    assert len(matrix) == 6
    assert all(len(row) == 6 for row in matrix)
    assert matrix[0][5] == 0
    assert matrix[5][1] == matrix[0][1]
    assert matrix[1][5] == matrix[1][0]
    assert matrix[0][1] == 2
    assert matrix[1][0] == 11


def test_incomplete_matrix_rejected(monkeypatch):
    def fake_post(url, payload, mask, timeout=45):
        return [
            item for item in fake_response(payload)
            if (item["originIndex"], item["destinationIndex"]) != (0, 1)
        ]

    monkeypatch.setattr(google_maps, "_post", fake_post)
    with pytest.raises(google_maps.MapsError):
        google_maps.route_matrix(sample_stops())


def test_element_error_rejected(monkeypatch):
    def fake_post(url, payload, mask, timeout=45):
        data = fake_response(payload)
        for item in data:
            if item["originIndex"] == 0 and item["destinationIndex"] == 1:
                item["status"] = {"code": 7, "message": "Denied"}
        return data

    monkeypatch.setattr(google_maps, "_post", fake_post)
    with pytest.raises(google_maps.MapsError):
        google_maps.route_matrix(sample_stops())


def test_matrix_limit_checked_before_network(monkeypatch):
    def forbidden_post(*args, **kwargs):
        raise AssertionError("Network helper must not be called")

    monkeypatch.setattr(google_maps, "_post", forbidden_post)
    stops = [
        {"name": str(i), "latitude": 35 + i * 0.001,
         "longitude": 14 + i * 0.001}
        for i in range(26)
    ]
    with pytest.raises(google_maps.MapsError):
        google_maps.route_matrix(stops)


def test_traffic_departure_in_batched_request(monkeypatch):
    calls = []

    def fake_post(url, payload, mask, timeout=45):
        calls.append(payload)
        return fake_response(payload)

    monkeypatch.setattr(google_maps, "_post", fake_post)
    when = datetime(2026, 11, 13, 8, 0,
                    tzinfo=ZoneInfo("Europe/Malta"))

    google_maps.route_matrix(
        sample_stops(), traffic=True, departure_at=when
    )

    assert len(calls) == 1
    assert calls[0]["routingPreference"] == "TRAFFIC_AWARE"
    assert calls[0]["departureTime"] == "2026-11-13T07:00:00Z"
