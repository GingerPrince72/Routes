"""Offline tests for bounded traffic-aware candidate comparison."""
from datetime import date
from traffic_candidates import compare_traffic_candidates


def fixtures():
    stops = [
        {'id':'TXP','name':'Depot'},
        {'id':1,'name':'A','service_minutes':5},
        {'id':2,'name':'B','service_minutes':5},
        {'id':'TXP','name':'Depot'},
    ]
    # Initial snapshot favours A first: 5+5+5 versus 8+5+8.
    matrix = [[0,5,8,0],[5,0,5,5],[8,8,0,5],[0,0,0,0]]
    return stops, matrix


def test_second_candidate_wins_under_actual_traffic():
    stops, matrix = fixtures()
    calls=[]
    def fake(a,b,when):
        calls.append((a['id'],b['id'],when.strftime('%H:%M')))
        if a['id']==1 and b['id']==2:
            return 50
        return 5
    route, stats=compare_traffic_candidates(stops,matrix,date(2026,11,13),'08:00',{},fake)
    assert [s['id'] for s in route]==['TXP',2,1,'TXP']
    assert route[-1]['departure']=='08:25'
    assert stats['google_leg_requests']==len(calls)
    assert stats['google_leg_requests']<=6


def test_hard_request_budget_keeps_valid_first_candidate():
    stops,matrix=fixtures()
    calls=[]
    def fake(a,b,when):
        calls.append(1)
        return 5
    route,stats=compare_traffic_candidates(stops,matrix,date(2026,11,13),'08:00',{},fake,max_requests=3)
    assert len(calls)==3
    assert stats['google_leg_requests']==3
    assert route[-1]['departure']=='08:25'
    assert stats['candidate_errors']
