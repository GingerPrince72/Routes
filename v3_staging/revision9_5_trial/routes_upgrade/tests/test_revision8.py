"""Offline route-order tests. No Google API calls."""
import datetime
from route_optimizer import optimise, windows_for_date


def test_exhaustive_prefers_earlier_finish_over_shorter_drive():
    stops = [
        {'id':'TXP','name':'Depot'},
        {'id':1,'name':'Late opener','service_minutes':5,'weekly_hours':'{"Thursday": [["09:00", "17:00"]]}'},
        {'id':2,'name':'Early opener','service_minutes':5,'weekly_hours':'{"Thursday": [["08:00", "17:00"]]}'},
        {'id':'TXP','name':'Depot'},
    ]
    matrix = [[0,5,10,0],[5,0,5,5],[10,5,0,10],[0,5,10,0]]
    windows=windows_for_date(stops,datetime.date(2026,11,12))
    result=optimise(stops,matrix,'08:00',12,windows)
    assert [s['id'] for s in result]==['TXP',2,1,'TXP']
    assert result[2]['wait_minutes']==40
    assert result[-1]['departure']=='09:10'


def test_split_opening_windows_wait_for_afternoon():
    stops=[{'id':'TXP','name':'Depot'},
           {'id':1,'name':'Split','service_minutes':10,'weekly_hours':'{"Thursday": [["08:00", "09:00"], ["10:00", "12:00"]]}'},
           {'id':'TXP','name':'Depot'}]
    matrix=[[0,10,0],[10,0,10],[0,10,0]]
    windows=windows_for_date(stops,datetime.date(2026,11,12))
    result=optimise(stops,matrix,'08:50',12,windows)
    assert result[1]['arrival']=='10:00'
    assert result[1]['wait_minutes']==60
    assert result[1]['departure']=='10:10'
    assert result[-1]['departure']=='10:20'


def test_equal_finish_prefers_less_driving():
    stops=[{'id':'TXP','name':'Depot'}, {'id':1,'name':'A','service_minutes':5},
           {'id':2,'name':'B','service_minutes':5}, {'id':'TXP','name':'Depot'}]
    matrix=[[0,5,10,0],[10,0,5,5],[10,20,0,10],[0,0,0,0]]
    result=optimise(stops,matrix,'08:00',12,{})
    assert [r['id'] for r in result]==['TXP',1,2,'TXP']
