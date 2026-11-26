"""Pure helpers for calculation fingerprints and Google Routes request budgets.

These estimates are HTTP-request upper bounds for the Revision 9.4 batching
implementation, not monetary quotes or a guarantee about Google's billing SKU.
"""

import hashlib
import json

MAX_MATRIX_ELEMENTS = 625


def estimate_requests(outlet_count, traffic, compare, candidate_count=2):
    """Return upper bound for one batched matrix and traffic leg lookups.

    Preserve the three-key interface used by Revision 9.2 and its tests.
    """
    if not isinstance(outlet_count, int) or isinstance(outlet_count, bool) or outlet_count < 0:
        raise ValueError('Outlet count must be a non-negative integer.')
    if not isinstance(candidate_count, int) or isinstance(candidate_count, bool) or not 2 <= candidate_count <= 4:
        raise ValueError('Traffic comparison must evaluate two to four candidates.')
    if outlet_count == 0:
        return {'matrix_requests': 0, 'leg_requests': 0, 'total_requests': 0}
    legs_per_order = outlet_count + 1
    if traffic and compare and outlet_count <= 5:
        leg_requests = candidate_count * legs_per_order
    elif traffic:
        leg_requests = legs_per_order
    else:
        leg_requests = 0
    return {'matrix_requests': 1, 'leg_requests': leg_requests,
            'total_requests': 1 + leg_requests}


def estimate_matrix_elements(outlet_count):
    """Google matrix size, with the duplicated TXP finish reused locally."""
    if not isinstance(outlet_count, int) or isinstance(outlet_count, bool) or outlet_count < 0:
        raise ValueError('Outlet count must be a non-negative integer.')
    return (outlet_count + 1) ** 2 if outlet_count else 0


def calculation_signature(ids, delivery_date, departure, max_hours, traffic,
                          compare, outlet_details=None, depot_details=None,
                          candidate_count=2):
    """Fingerprint all route-defining inputs without including credentials."""
    payload = {
        'algorithm_version': 'revision9_4_precise_v1',
        'ids': list(ids),
        'date': str(delivery_date),
        'departure': str(departure).strip(),
        'max_hours': str(float(max_hours)),  # Do not truncate fractional hours.
        'traffic': bool(traffic),
        'compare': bool(compare and traffic),
        'candidate_count': int(candidate_count) if compare and traffic else 0,
        'outlets': outlet_details,
        'depot': depot_details,
    }
    serialized = json.dumps(payload, sort_keys=True, default=str,
                            ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(serialized.encode('utf-8')).hexdigest()


def is_duplicate(signature, last_success_signature, override):
    """Compatibility helper; the durable journal performs actual enforcement."""
    return bool(signature and last_success_signature and
                signature == last_success_signature and not override)
