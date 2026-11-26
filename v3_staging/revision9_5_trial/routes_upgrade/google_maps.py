"""Google Places and Routes helpers for the Malta delivery planner."""

import math
import os
from decimal import Decimal, InvalidOperation
from datetime import timezone
from urllib.parse import urlencode

import requests

PLACES_ENDPOINT = 'https://places.googleapis.com/v1/places:searchText'
ROUTES_ENDPOINT = 'https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix'
MAX_MATRIX_ELEMENTS = 625  # DRIVE + TRAFFIC_UNAWARE / TRAFFIC_AWARE; not OPTIMAL.
MATRIX_FIELDS = 'originIndex,destinationIndex,duration,condition,status'


class MapsError(RuntimeError):
    pass


def key():
    value = os.getenv('GOOGLE_MAPS_API_KEY', '').strip()
    if not value:
        raise MapsError('GOOGLE_MAPS_API_KEY is not available in this Codespace.')
    return value


def _post(url, payload, mask, timeout=45):
    try:
        response = requests.post(
            url,
            json=payload,
            headers={
                'X-Goog-Api-Key': key(),
                'X-Goog-FieldMask': mask,
                'Content-Type': 'application/json',
            },
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 'unknown'
        try:
            message = exc.response.json().get('error', {}).get('message', 'API request rejected')
        except Exception:
            message = 'API request rejected'
        raise MapsError(f'Google API HTTP {status}: {message}') from None
    except (requests.exceptions.RequestException, ValueError) as exc:
        raise MapsError(f'Google API connection/response error: {exc}') from exc


def _parse_periods(place):
    """Google Places weekday numbering is Sunday=0 through Saturday=6."""
    regular = place.get('regularOpeningHours')
    if not regular:
        return None
    days = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
    periods = regular.get('periods')
    if not periods:
        return None
    result = {day: 'CLOSED' for day in days}
    for period in periods:
        opening, closing = period.get('open') or {}, period.get('close') or {}
        if not all(field in opening for field in ('day', 'hour')):
            continue
        if not all(field in closing for field in ('day', 'hour')):
            continue
        day_num = opening['day']
        if not isinstance(day_num, int) or not 0 <= day_num <= 6:
            continue
        if closing['day'] != day_num:
            continue  # Overnight opening needs separate-day handling.
        start = f"{opening['hour']:02d}:{opening.get('minute', 0):02d}"
        end = f"{closing['hour']:02d}:{closing.get('minute', 0):02d}"
        day = days[day_num]
        if result[day] == 'CLOSED':
            result[day] = []
        result[day].append([start, end])
    return result


def search_place(query):
    q = query.strip()
    if 'malta' not in q.casefold():
        q += ', Malta'
    payload = {
        'textQuery': q, 'pageSize': 5, 'regionCode': 'MT',
        'languageCode': 'en',
        'locationBias': {'circle': {
            'center': {'latitude': 35.9375, 'longitude': 14.3754},
            'radius': 25000.0,
        }},
    }
    data = _post(
        PLACES_ENDPOINT, payload,
        'places.id,places.displayName,places.formattedAddress,places.location,'
        'places.regularOpeningHours.periods,places.regularOpeningHours.weekdayDescriptions',
    )
    out = []
    for place in data.get('places', []):
        loc = place.get('location')
        if not loc:
            continue
        out.append({
            'name': place.get('displayName', {}).get('text', ''),
            'place_id': place.get('id', ''),
            'address': place.get('formattedAddress', ''),
            'latitude': loc['latitude'],
            'longitude': loc['longitude'],
            'weekly_hours': _parse_periods(place) or {},
        })
    return out


def _coordinate(stop):
    """Validate coordinates before starting any billable request."""
    try:
        lat = float(stop['latitude'])
        lon = float(stop['longitude'])
    except (KeyError, TypeError, ValueError) as exc:
        raise MapsError(f'Invalid coordinates for {stop.get("name", "location")}.') from exc
    if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        raise MapsError(f'Invalid coordinates for {stop.get("name", "location")}.')
    return lat, lon


def _status_error(item):
    status = item.get('status', {})
    if status is None:
        return False
    if not isinstance(status, dict):
        return True
    # Proto3 can omit the successful default code (0).
    return status.get('code', 0) not in (0, '0')


def _minutes_from_duration(duration):
    """Parse protobuf Duration seconds exactly into Decimal minutes.

    Keep sub-minute precision (including legitimate zero-second short legs).
    Callers MUST round only for human presentation or integer-second solvers.
    """
    if not isinstance(duration, str) or not duration.endswith('s'):
        raise MapsError('Invalid Google journey duration.')
    try:
        seconds = Decimal(duration[:-1])
    except (InvalidOperation, ValueError) as exc:
        raise MapsError('Invalid Google journey duration.') from exc
    if not seconds.is_finite() or seconds < 0:
        raise MapsError('Invalid Google journey duration.')
    return seconds / Decimal(60)


def route_matrix(stops, traffic=False, departure_at=None):
    """Fetch one batched DRIVE matrix in PRECISE Decimal minutes.

    Reuse TXP for matching start/end positions.

    Matrix indices remain aligned with ``stops``. Start and finish can refer to
    the same coordinates without asking Google for a duplicate depot row/column.
    """
    if traffic and (departure_at is None or departure_at.tzinfo is None):
        raise MapsError('Traffic-aware routing requires a timezone-aware departure datetime.')

    n = len(stops)
    if n < 2:
        raise MapsError('At least two locations are required.')

    coordinates = [_coordinate(stop) for stop in stops]
    depot_repeats = coordinates[-1] == coordinates[0]
    unique = stops[:-1] if depot_repeats else stops
    m = len(unique)
    if m * m > MAX_MATRIX_ELEMENTS:
        raise MapsError(
            f'Route matrix requires {m * m} elements; Google limit is '
            f'{MAX_MATRIX_ELEMENTS}. Split the route before calculating.'
        )

    # If there are no distinct destinations, no network request is necessary.
    if m == 1:
        return [[0] * n for _ in range(n)]

    waypoints = [
        {'waypoint': {'location': {'latLng': {
            'latitude': lat, 'longitude': lon,
        }}}}
        for lat, lon in coordinates[:m]
    ]
    payload = {
        'origins': waypoints,
        'destinations': waypoints,
        'travelMode': 'DRIVE',
        'routingPreference': 'TRAFFIC_AWARE' if traffic else 'TRAFFIC_UNAWARE',
    }
    if traffic:
        payload['departureTime'] = (
            departure_at.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
        )

    rows = _post(ROUTES_ENDPOINT, payload, MATRIX_FIELDS)
    if not isinstance(rows, list):
        raise MapsError('Unexpected Routes matrix response.')

    unique_matrix = [[0 if i == j else None for j in range(m)] for i in range(m)]
    seen = set()
    for item in rows:
        if not isinstance(item, dict):
            raise MapsError('Invalid Google matrix element.')
        i, j = item.get('originIndex'), item.get('destinationIndex')
        if not (type(i) is int and type(j) is int and 0 <= i < m and 0 <= j < m):
            raise MapsError('Invalid Google matrix element indices.')
        if (i, j) in seen:
            raise MapsError('Duplicate Google matrix element.')
        seen.add((i, j))
        if i == j:
            continue  # A location to itself has zero travel time.
        if _status_error(item):
            raise MapsError(f'Google could not route {unique[i]["name"]} → {unique[j]["name"]}.')
        if item.get('condition') != 'ROUTE_EXISTS':
            raise MapsError(f'No usable driving route for {unique[i]["name"]} → {unique[j]["name"]}.')
        unique_matrix[i][j] = _minutes_from_duration(item.get('duration'))

    if any(value is None for row in unique_matrix for value in row):
        raise MapsError('Incomplete Google Routes matrix; optimisation cancelled.')

    def source_index(index):
        return 0 if depot_repeats and index == n - 1 else index

    return [
        [unique_matrix[source_index(i)][source_index(j)] for j in range(n)]
        for i in range(n)
    ]


def traffic_leg_minutes(origin, destination, departure_at):
    """Request one traffic-aware journey at its actual planned departure time."""
    if departure_at.tzinfo is None:
        raise MapsError('Leg departure requires a timezone-aware datetime.')
    payload = {
        'origins': [{'waypoint': {'location': {'latLng': {
            'latitude': origin['latitude'], 'longitude': origin['longitude'],
        }}}}],
        'destinations': [{'waypoint': {'location': {'latLng': {
            'latitude': destination['latitude'], 'longitude': destination['longitude'],
        }}}}],
        'travelMode': 'DRIVE',
        'routingPreference': 'TRAFFIC_AWARE',
        'departureTime': departure_at.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z'),
    }
    items = _post(ROUTES_ENDPOINT, payload, MATRIX_FIELDS)
    if not isinstance(items, list) or len(items) != 1:
        raise MapsError(f'No traffic-aware route for {origin["name"]} → {destination["name"]}.')
    item = items[0]
    if not isinstance(item, dict) or _status_error(item) or item.get('condition') != 'ROUTE_EXISTS':
        raise MapsError(f'No traffic-aware route for {origin["name"]} → {destination["name"]}.')
    if item.get('originIndex') != 0 or item.get('destinationIndex') != 0:
        raise MapsError('Invalid traffic-aware leg response.')
    return _minutes_from_duration(item.get('duration'))


def _maps_location_label(stop):
    """Prefer searchable business identity over a coordinate-only dropped pin.

    Google Maps URL parameters accept place names and addresses. A Place ID,
    when known, resolves the exact establishment. This function deliberately
    does not append lat/lng to a business name: Google's Maps URL documentation
    warns that names combined with raw coordinates may still show as pins.
    """
    name = str(stop.get('name') or '').strip()
    address = str(stop.get('address') or '').strip()
    if name and address and name.casefold() not in address.casefold():
        return f'{name}, {address}'
    if name or address:
        return name or address
    lat, lon = _coordinate(stop)
    return f'{lat:.7f},{lon:.7f}'


def _maps_place_id(stop):
    return str(stop.get('place_id') or '').strip()


def navigation_url(stops):
    """Build a named Google Maps directions URL, without any API requests.

    Maps URLs support at most three via-points in some mobile browsers, even
    though desktop Maps can handle more. The UI also offers individual links.
    """
    if len(stops) < 2:
        return None
    args = {
        'api': '1',
        'origin': _maps_location_label(stops[0]),
        'destination': _maps_location_label(stops[-1]),
        'travelmode': 'driving',
    }
    origin_id = _maps_place_id(stops[0])
    destination_id = _maps_place_id(stops[-1])
    if origin_id:
        args['origin_place_id'] = origin_id
    if destination_id:
        args['destination_place_id'] = destination_id
    if len(stops) > 2:
        via = stops[1:-1]
        args['waypoints'] = '|'.join(_maps_location_label(point) for point in via)
        ids = [_maps_place_id(point) for point in via]
        # Google requires waypoint_place_ids to match each waypoint exactly.
        if all(ids):
            args['waypoint_place_ids'] = '|'.join(ids)
    return 'https://www.google.com/maps/dir/?' + urlencode(args)


def navigate_to_stop_url(stop):
    """One-tap directions from the phone's current location to a named stop.

    Each URL has one destination and no via-points, so iOS mobile-browser
    waypoint limits cannot drop a delivery. No Google Cloud API call is made.
    """
    args = {
        'api': '1',
        'destination': _maps_location_label(stop),
        'travelmode': 'driving',
    }
    place_id = _maps_place_id(stop)
    if place_id:
        args['destination_place_id'] = place_id
    return 'https://www.google.com/maps/dir/?' + urlencode(args)
