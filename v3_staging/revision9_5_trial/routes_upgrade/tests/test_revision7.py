"""Revision 7 regression checks; no network requests."""
import datetime
import pytest
from route_optimizer import optimise, windows_for_date, OptimisationError


def _stops():
    return [
        {'id':'TXP','name':'TXP','service_minutes':0},
        {'id':1,'name':'Opens later','service_minutes':5,'weekly_hours':'{"Thursday": [["09:00", "17:00"]]}'},
        {'id':2,'name':'Open early','service_minutes':5,'weekly_hours':'{"Thursday": [["08:00", "17:00"]]}'},
        {'id':'TXP','name':'TXP','service_minutes':0},
    ]


def test_earliest_completion_includes_waiting():
    stops=_stops()
    matrix=[[0,5,10,0],[5,0,5,5],[10,5,0,10],[0,5,10,0]]
    windows=windows_for_date(stops,datetime.date(2026,11,12))
    result=optimise(stops,matrix,'08:00',12,windows,solve_seconds=2)
    assert result[1]['name']=='Open early'
    assert result[-1]['name']=='TXP'


def test_closed_outlet_rejected():
    stops=_stops()
    stops[1]['weekly_hours']='{"Thursday": "CLOSED"}'
    windows=windows_for_date(stops,datetime.date(2026,11,12))
    matrix=[[0,5,10,0],[5,0,5,5],[10,5,0,10],[0,5,10,0]]
    with pytest.raises(OptimisationError,match='closed'):
        optimise(stops,matrix,'08:00',12,windows,solve_seconds=2)
