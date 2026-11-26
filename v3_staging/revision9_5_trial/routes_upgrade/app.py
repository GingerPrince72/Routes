import csv, io, json, os, sqlite3, math
from contextlib import closing
from html import escape
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import streamlit as st
from database import DB_PATH, all_locations, delete_location, get_setting, init_db, save_setting, upsert_location
from google_maps import (MapsError, navigation_url, navigate_to_stop_url,
                         route_matrix, search_place, traffic_leg_minutes)
from route_optimizer import OptimisationError, optimise, minutes, windows_for_date, validate_traffic_route
from traffic_candidates import compare_traffic_candidates
from route_analysis import snapshot_report
from request_guard import (estimate_requests, estimate_matrix_elements,
                           MAX_MATRIX_ELEMENTS, calculation_signature)
from calculation_journal import (CalculationJournal, GuardBlocked,
                                 RequestBudgetExceeded, RECOVERY_MINUTES)

st.set_page_config(page_title='TXP Routes', page_icon='🚚', layout='wide', initial_sidebar_state='collapsed')
init_db()
title_column, new_route_column = st.columns([8, 1], gap='small')
with title_column:
    st.title('TXP Routes')
with new_route_column:
    start_new_route = st.button('＋', key='new_route_top', help='Start a new route')
if start_new_route:
    st.session_state.confirm_new_route = True
    st.session_state.page = 'Route Planner'
# Mobile-only density changes; all controls remain accessible, no unsafe JS.
st.markdown("""
<style>
/* Two blue shades replace the original dark grey backgrounds. */
.stApp {
  --background-color: #0d2339;
  --secondary-background-color: #204660;
}
.stApp, .stApp [data-testid="stAppViewContainer"],
.stApp [data-testid="stHeader"], .stApp [data-testid="stSidebar"] {
  background-color: #0d2339 !important;
}
.stApp [data-testid="stExpander"],
.stApp [data-testid="stExpander"] > details > summary,
.stApp [data-testid="stBaseButton-secondary"],
.stApp [data-testid="stTextInputRootElement"],
.stApp [data-testid="stTextAreaRootElement"],
.stApp [data-testid="stDateInputField"],
.stApp [data-testid="stNumberInputContainer"],
.stApp [data-testid="stSelectbox"] [role="group"],
.stApp [data-baseweb="input"],
.stApp [data-baseweb="textarea"],
.stApp [data-baseweb="select"] > div {
  background-color: #204660 !important;
  border-color: #416d87 !important;
}
.stApp [data-testid="stExpander"] > details > summary,
.stApp [data-testid="stExpander"] > details > summary * {
  color: #f1f7fa !important;
}
.stApp [data-testid="stDateInputField"] input,
.stApp [data-baseweb="input"] input,
.stApp [data-baseweb="textarea"] textarea {
  color: #f1f7fa !important;
}
/* Keep the route action alongside the title on narrow screens. */
.stApp [data-testid="stHorizontalBlock"]:has(.st-key-new_route_top) {
  flex-wrap: nowrap !important; align-items: center; gap: .5rem !important;
}
.stApp [data-testid="stHorizontalBlock"]:has(.st-key-new_route_top) > [data-testid="stColumn"]:first-child {
  flex: 1 1 auto !important; width: auto !important; min-width: 0 !important;
}
.stApp [data-testid="stHorizontalBlock"]:has(.st-key-new_route_top) > [data-testid="stColumn"]:last-child {
  flex: 0 0 44px !important; width: 44px !important; min-width: 44px !important;
}
.stApp .st-key-new_route_top button {
  width: 44px !important; height: 44px !important; min-height: 44px !important;
  padding: 0 !important; border-radius: 50% !important; font-size: 1.4rem;
  margin-top: .8rem;
}
.stApp .st-key-new_route_top button p { font-size: 1.4rem !important; line-height: 1 !important; }
@media (max-width: 700px) {
  .block-container {
    padding-top: 3.2rem !important;
    padding-left: max(1rem, calc(env(safe-area-inset-left, 0px) + .75rem)) !important;
    padding-right: max(1rem, calc(env(safe-area-inset-right, 0px) + .75rem)) !important;
    padding-bottom: 1.8rem !important;
    box-sizing: border-box;
    max-width: 100%;
    min-width: 0;
  }
  .stApp h1 {font-size: 1.45rem !important; margin-bottom: .10rem !important;}
  .stApp h2 {font-size: 1.18rem !important; margin-top: .45rem;}
  .stApp h3 {font-size: 1.02rem !important; margin-top: .45rem;}
  div[data-testid="stVerticalBlock"] {gap: .75rem;}
  div[data-testid="stElementContainer"] {margin-bottom: 0 !important;}
  .stApp div[data-testid="stAlert"] {padding: .45rem .55rem;}
  .stApp .stButton button, .stApp .stLinkButton a {min-height: 43px;}
}
.txp-quick-route {display: flex;flex-direction: column;gap: 3px;margin: .3rem 0 .5rem;}
.txp-quick-stop {display:flex;align-items:center;gap:.65rem;padding:.43rem .6rem;
    background:var(--secondary-background-color,#f2f3f5);
    border-radius: .45rem;line-height:1.28;}
.txp-quick-time {width: 4.8rem;flex-shrink:0;font-variant-numeric:tabular-nums;font-weight:700;}
.txp-quick-name {min-width:0;overflow-wrap:anywhere;}
</style>
""", unsafe_allow_html=True)

def active_ids():
    try:
        ids = json.loads(get_setting('active_route_ids') or '[]')
        return [int(x) for x in ids]
    except (ValueError, TypeError):
        return []

def set_active_ids(ids):
    save_setting('active_route_ids', json.dumps(list(dict.fromkeys(int(x) for x in ids))))
    st.session_state.pop('last_route', None)

def add_to_route(loc_id):
    ids = active_ids()
    if loc_id not in ids:
        set_active_ids(ids + [loc_id])
        st.session_state.route_notice = 'Outlet added to the current route.'
    else:
        st.session_state.route_notice = 'This outlet is already in the current route.'
    st.session_state['outlet_query'] = ''
    st.session_state.places_matches = []
    st.rerun()

def save_google_result_to_route(place):
    """Save or reuse a Google place and add it to the route atomically."""
    name = (place.get('name') or '').strip()
    address = (place.get('address') or '').strip()
    place_id = (place.get('place_id') or '').strip()
    hours = place.get('weekly_hours') or {}
    if not name or not isinstance(hours, dict):
        raise ValueError('Google did not return a valid outlet name or opening hours.')
    try:
        latitude = float(place['latitude'])
        longitude = float(place['longitude'])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('Google did not return valid coordinates for this outlet.') from exc
    if not (math.isfinite(latitude) and math.isfinite(longitude)
            and -90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError('Google returned invalid coordinates for this outlet.')

    with closing(sqlite3.connect(DB_PATH, timeout=15)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN IMMEDIATE')
        existing = db.execute(
            'SELECT id, name, address, place_id FROM locations '
            'WHERE COALESCE(is_depot, 0) = 0 ORDER BY id'
        ).fetchall()
        match = next((row for row in existing if place_id and
                      (row['place_id'] or '').strip() == place_id), None)
        if match is None:
            match = next((row for row in existing if
                          (row['name'] or '').strip().casefold() == name.casefold() and
                          (row['address'] or '').strip().casefold() == address.casefold()), None)
        setting = db.execute("SELECT value FROM settings WHERE key='active_route_ids'").fetchone()
        try:
            saved_ids = json.loads(setting['value']) if setting else []
            if not isinstance(saved_ids, list):
                raise ValueError('Route list is not a list.')
            route_ids = [int(item) for item in saved_ids]
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError('Could not read the current route; no outlet was changed.') from exc
        if match is None:
            cursor = db.execute(
                'INSERT INTO locations (name, address, latitude, longitude, service_minutes, '
                'notes, weekly_hours, place_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (name, address, latitude, longitude, 5, '', json.dumps(hours), place_id),
            )
            outlet_id = cursor.lastrowid
            created = True
        else:
            outlet_id = int(match['id'])
            created = False
        added = outlet_id not in route_ids
        if added:
            route_ids.append(outlet_id)
            db.execute(
                "INSERT INTO settings(key, value) VALUES('active_route_ids', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(route_ids),),
            )

    if added:
        st.session_state.pop('last_route', None)
    st.session_state.clear_outlet_query = True
    st.session_state.places_matches = []
    st.session_state.new_place = None
    st.session_state.show_new_outlet = False
    if created:
        st.session_state.save_notice = f'Outlet saved and added to route: {name}'
    elif added:
        st.session_state.save_notice = f'Existing outlet added to route: {name}'
    else:
        st.session_state.save_notice = f'This outlet is already in your route: {name}'
    st.rerun()


if st.session_state.get('route_notice'):
    st.info(st.session_state.pop('route_notice'))
if st.session_state.get('save_notice'):
    st.success(st.session_state.pop('save_notice'))
st.caption('Malta · delivery routes · Google Maps')


def parse_optional_time(value, label):
    value = (value or '').strip()
    if not value:
        return None
    try:
        minutes(value)
    except Exception:
        raise ValueError(f'{label} must be HH:MM or blank.')
    return value


def editor(default=None, prefix='new'):
    default = default or {}
    hours = default.get('weekly_hours') or {}
    if isinstance(hours, str):
        try:
            hours = json.loads(hours)
        except (ValueError, TypeError):
            hours = {}
    # Change widget keys when a different Google result is chosen so Streamlit
    # doesn't retain the previous business's stale form values.
    form_version = st.session_state.get('new_place_version', 0) if prefix == 'new_outlet' else 0
    widget_prefix = f'{prefix}_{form_version}'
    with st.form(widget_prefix):
        name = st.text_input('Outlet name', value=default.get('name') or '', key=f'{widget_prefix}_name')
        address = st.text_input('Address', value=default.get('address') or '', key=f'{widget_prefix}_address')
        a,b = st.columns(2)
        lat = a.number_input('Latitude', min_value=-90., max_value=90., value=float(default.get('latitude') or 35.90), format='%.6f', key=f'{widget_prefix}_lat')
        lon = b.number_input('Longitude', min_value=-180., max_value=180., value=float(default.get('longitude') or 14.50), format='%.6f', key=f'{widget_prefix}_lon')
        st.markdown('**Opening hours**')
        st.caption('Google hours are prefilled when available. Leave a day blank if unknown or unrestricted. You can edit each day.')
        day_names = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']
        weekly = {}
        for i in range(0,7,2):
            cols = st.columns(2)
            for col, day in zip(cols, day_names[i:i+2]):
                current = hours.get(day, [])
                value = 'CLOSED' if current == 'CLOSED' else ', '.join(f'{p[0]}-{p[1]}' for p in current)
                weekly[day] = col.text_input(f'{day} (HH:MM-HH:MM; multiple periods comma-separated)', value=value, key=f'{widget_prefix}_{day}')
        service = st.number_input('Unload minutes', min_value=0, max_value=240, value=int(default.get('service_minutes') if default.get('service_minutes') is not None else 5), key=f'{widget_prefix}_service')
        notes = st.text_area('Notes', value=default.get('notes') or '', key=f'{widget_prefix}_notes')
        submitted = st.form_submit_button('Save outlet')
        if submitted:
            try:
                if not name.strip(): raise ValueError('Enter an outlet name.')
                if not (-90 <= lat <= 90 and -180 <= lon <= 180): raise ValueError('Invalid coordinates.')
                parsed = {}
                for day, raw in weekly.items():
                    periods=[]
                    if raw.strip().casefold() == 'closed':
                        periods='CLOSED'
                    elif raw.strip():
                        for part in raw.split(','):
                            pair=part.strip().split('-',1)
                            if len(pair)!=2: raise ValueError(f'{day}: use HH:MM-HH:MM.')
                            start,end=pair[0].strip(),pair[1].strip()
                            if minutes(end) <= minutes(start): raise ValueError(f'{day}: closing must be after opening.')
                            periods.append([start,end])
                    parsed[day]=periods
                new_id = upsert_location({'name':name.strip(),'address':address.strip(),'latitude':lat,'longitude':lon,
                    'weekly_hours':json.dumps(parsed),'service_minutes':int(service),
                    'notes':notes,'place_id':default.get('place_id') or ''}, default.get('id'))
                if prefix == 'new_outlet' and new_id is not None:
                    ids = active_ids()
                    set_active_ids(ids + [new_id])
                st.session_state.save_notice = f'Outlet saved and added to route: {name.strip()}' if prefix == 'new_outlet' else f'Outlet saved: {name.strip()}'
                if prefix == 'new_outlet': st.session_state.clear_outlet_query = True
                st.session_state.new_place = None
                st.session_state.show_new_outlet = False
                st.session_state.places_matches = []
                st.rerun()
            except ValueError as exc: st.error(str(exc))

locations=all_locations()
by_id={int(loc['id']):loc for loc in locations}
ids=[i for i in active_ids() if i in by_id]
if ids != active_ids(): set_active_ids(ids)
route_locations=[by_id[i] for i in ids]
page = st.sidebar.radio('Navigation', ['Route Planner', 'Saved Outlets', 'Settings / Backup'], key='page')
if page == 'Route Planner':
    st.subheader('Your delivery route')
    if st.session_state.get('confirm_new_route'):
        st.warning('Start a new route? This clears the current stop list and optimisation result, but keeps every saved outlet.')
        x,y=st.columns(2)
        if x.button('Confirm new route'):
            set_active_ids([])
            save_setting('active_route_name','New delivery route')
            st.session_state.pop('route_name',None)
            st.session_state.confirm_new_route=False
            st.session_state.route_notice='New route started. Your outlet database is unchanged.'
            st.rerun()
        if y.button('Cancel'): st.session_state.confirm_new_route=False;st.rerun()
    with st.expander('Add / find a delivery stop', expanded=False):
        st.subheader('Find or add an outlet')
        st.caption(f'{len(route_locations)} delivery stop(s) currently in this route.')
        st.caption('Search SQLite first. Google is contacted only when you click Search Google Places.')
        if st.session_state.pop('clear_outlet_query', False):
            st.session_state['outlet_query'] = ''
        query=st.text_input('Outlet name or address',placeholder='Type Krypton or Swieqi',key='outlet_query')
        q=query.strip().casefold()
        local_matches=[loc for loc in locations if q and (q in (loc.get('name') or '').casefold() or q in (loc.get('address') or '').casefold())]
        if q:
            if local_matches:
                st.success(f'{len(local_matches)} saved outlet(s) found. No Google request made.')
                for loc in local_matches:
                    c1,c2=st.columns([5,2])
                    c1.write(f"**{loc['name']}** · {loc.get('address') or 'No address'}")
                    c2.button('Already in route' if loc['id'] in ids else 'Add to route',key=f'add_{loc["id"]}',disabled=loc['id'] in ids,on_click=add_to_route,args=(loc['id'],))
            else: st.info('No saved outlet matches. Search Google Places below if needed.')
        else: st.info(f'{len(locations)} saved outlets. Type a name or address to search locally.')
        with st.expander('Search Google Places (may incur API charges)',expanded=False):
            if st.button('Search Google Places',disabled=not q):
                try:
                    st.session_state.places_matches=search_place(query)
                    st.session_state.places_query=query
                except MapsError as exc:st.error(str(exc))
            matches=st.session_state.get('places_matches',[]) if st.session_state.get('places_query')==query else []
            if matches:
                labels=[f"{p['name']} | {p['address']}" for p in matches]
                selected=st.selectbox('Select the correct business',range(len(matches)),format_func=lambda i:labels[i])
                chosen=matches[selected]
                chosen_place_id = (chosen.get('place_id') or '').strip()
                duplicates = [loc for loc in locations if
                    (chosen_place_id and (loc.get('place_id') or '').strip() == chosen_place_id) or
                    ((loc.get('name') or '').strip().casefold() == (chosen.get('name') or '').strip().casefold()
                     and (loc.get('address') or '').strip().casefold() == (chosen.get('address') or '').strip().casefold())]
                if duplicates:
                    st.info('This outlet is already saved.')
                    st.button('Add saved outlet to route', key='add_saved_google',
                              disabled=duplicates[0]['id'] in ids, on_click=add_to_route,
                              args=(duplicates[0]['id'],))
                else:
                    st.write('**Google opening hours returned:**')
                    if chosen.get('weekly_hours'):
                        for day,periods in chosen['weekly_hours'].items():
                            st.write(f"{day}: {'Closed' if periods=='CLOSED' else (', '.join(a+'–'+b for a,b in periods) if periods else 'No hours returned')}")
                    else:st.info('Google did not return regular opening hours.')
                    if st.button('Save and add to route', key='save_google_to_route'):
                        try:
                            save_google_result_to_route(chosen)
                        except (ValueError, TypeError, sqlite3.Error, OSError) as exc:
                            st.error(f'Could not save this outlet and add it to the route: {exc}')
        if not st.session_state.get('show_new_outlet', False):
            if st.button('Add outlet manually', key='show_manual_outlet'):
                st.session_state.new_place = None
                st.session_state.new_place_version = st.session_state.get('new_place_version', 0) + 1
                st.session_state.show_new_outlet = True
                st.rerun()
        else:
            st.subheader('New outlet details')
            if st.button('Cancel new outlet', key='cancel_new_outlet'):
                st.session_state.new_place = None
                st.session_state.show_new_outlet = False
                st.rerun()
            editor(st.session_state.get('new_place'), 'new_outlet')
    with st.expander(f'Edit delivery stops ({len(route_locations)})', expanded=False):
        st.caption('Edit the current order, remove stops or review addresses.')
        depot_raw=get_setting('depot_json')
        depot=json.loads(depot_raw) if depot_raw else None
        if depot:
            st.markdown(f"**START: {depot['name']}**")
        else:
            st.warning('Configure TXP in Settings / Backup before optimising.')
        if not route_locations:
            st.info('No deliveries yet. Use Add / Find above to build your route.')
        for pos,loc in enumerate(route_locations):
            st.write(f"**{pos+1}. {loc['name']}**  ·  {loc.get('address') or 'No address'}  ·  {loc.get('service_minutes',5)} min")
        if route_locations:
            selected_stop = st.selectbox(
                'Choose a stop to change', ids,
                format_func=lambda stop_id: by_id[stop_id]['name'],
                key='compact_selected_stop',
            )
            stop_action = st.radio(
                'Stop action', ['Move up', 'Move down', 'Remove from route'],
                horizontal=True, key='compact_stop_action',
            )
            selected_position = ids.index(selected_stop)
            action_unavailable = (
                (stop_action == 'Move up' and selected_position == 0) or
                (stop_action == 'Move down' and selected_position == len(ids) - 1)
            )
            if st.button('Apply stop action', disabled=action_unavailable):
                if stop_action == 'Remove from route':
                    set_active_ids([i for i in ids if i != selected_stop])
                else:
                    target = selected_position + (-1 if stop_action == 'Move up' else 1)
                    ids[selected_position], ids[target] = ids[target], ids[selected_position]
                    set_active_ids(ids)
                st.rerun()
        if depot: st.markdown(f"**FINISH: {depot['name']}**")
        st.caption('Delete removes a stop from this route only. All saved outlet details remain in SQLite.')
    st.subheader('Optimise this route')
    delivery_date=st.date_input('Delivery date',value=date.today())
    departure=st.text_input('Departure time (HH:MM)',value='08:00')
    with st.expander('Route duration limit', expanded=False):
        max_hours=st.number_input('Maximum route duration (hours)',1,20,12)
    standard_mode = 'Standard · 1 Google matrix request'
    traffic_mode = 'Traffic-aware · matrix + journey legs'
    compare_mode = 'Compare 2–4 traffic-aware orders'
    modes = [standard_mode, traffic_mode]
    if len(route_locations) <= 5:
        modes.append(compare_mode)
    selected_mode = st.selectbox(
        'Driving-time mode', modes, index=0, key='txp_driving_mode',
        help='Standard compares every small-route order using one consistent matrix. '
             'Traffic-aware modes need additional potentially billable Google requests.',
    ) or standard_mode
    traffic = selected_mode != standard_mode
    compare_orders = selected_mode == compare_mode
    if traffic:
        st.warning('Traffic-aware mode uses extra potentially billable Google requests. '
                   'The result depends on the selected departure time and cannot '
                   'guarantee a global time-dependent optimum.')
    if len(route_locations) > 5:
        st.caption('Multiple traffic-aware candidates require five or fewer outlets.')
    # Explicitly opt into more candidates. Default remains the historical 2.
    candidate_count = 2
    if traffic and compare_orders and len(route_locations) <= 5:
        candidate_count = st.selectbox(
            'Traffic-aware candidate orders to compare', options=[2, 3, 4], index=0,
            help='For 3–4 candidates, include alternatives finishing at different outlets, such as Krypton-last. This is NOT global time-dependent optimisation.')
        st.warning(
            f'Up to {candidate_count * (len(route_locations) + 1)} individual Google '
            f'traffic-leg requests PLUS the initial matrix request. '
            'The actual number may be lower. Check the budget below before calculating.')
    def hours_unverified(loc, day):
        try:
            raw = loc.get('weekly_hours') or '{}'
            schedule = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(schedule, dict):
                return True
            periods = schedule.get(day.strftime('%A'))
            return periods is None or periods == [] or periods == ''
        except (ValueError, TypeError, AttributeError):
            return True
    unverified_names = [loc['name'] for loc in route_locations if hours_unverified(loc, delivery_date)]
    if unverified_names:
        st.warning('PROVISIONAL SCHEDULE: opening hours unverified for '+', '.join(unverified_names)+'. Confirm these outlets are open before delivery.')
    # Disabled Streamlit widgets can retain a previous value on some reruns.
    # Derive an unequivocal effective mode for signatures, budgets and execution.
    compare_enabled = bool(traffic and compare_orders and len(route_locations) <= 5)
    estimate = estimate_requests(len(route_locations), traffic, compare_enabled,
                                 candidate_count=candidate_count)
    matrix_elements = estimate_matrix_elements(len(route_locations))
    total_word = 'request' if estimate['total_requests'] == 1 else 'requests'
    leg_word = 'request' if estimate['leg_requests'] == 1 else 'requests'
    st.markdown(
        f'**Google Routes budget: up to {estimate["total_requests"]} HTTP {total_word}** '
        f'({estimate["matrix_requests"]} batched matrix request + '
        f'{estimate["leg_requests"]} traffic leg {leg_word}).'
    )
    st.markdown(
        f'**Matrix elements: {matrix_elements}** '
        f'(one TXP depot plus {len(route_locations)} outlet positions; '
        'TXP is reused for the finish).'
    )
    st.caption(
        'Upper bounds, not a price quote. Google bills by applicable SKU and '
        'elements. Google Places searches are separate. No request is made '
        'until you explicitly calculate.'
    )
    if matrix_elements > MAX_MATRIX_ELEMENTS:
        st.error(f'This route exceeds the Google matrix limit of '
                 f'{MAX_MATRIX_ELEMENTS} elements. Split the route before calculating.')

    signature = calculation_signature(
        ids, delivery_date, departure, max_hours, traffic, compare_enabled,
        route_locations, depot, candidate_count=candidate_count,
    )
    journal = None
    latest = None
    saved = None
    try:
        journal = CalculationJournal()
        latest = journal.latest(signature)
        saved = journal.last_success(signature)
    except (sqlite3.Error, OSError, ValueError) as exc:
        st.error('The separate calculation journal is unavailable. '
                 'To prevent accidental Google charges, calculation is disabled. '
                 f'Details: {exc}')

    already_attempted = latest is not None
    active_attempt = bool(latest and latest['state'] == 'in_progress')
    retry_key = f'allow_route_repeat_{signature}_{latest["id"] if latest else "new"}'
    authorised_repeat = st.checkbox(
        'I authorise another calculation for these exact inputs '
        '(Google may charge again)',
        key=retry_key, disabled=not already_attempted or active_attempt,
    )
    if active_attempt:
        st.warning('This exact calculation was already started and may have '
                   'contacted Google. No second calculation is permitted while '
                   'it is marked in progress.')
        if journal is not None and journal.is_stale(latest):
            acknowledgement = st.checkbox(
                'I confirm the earlier calculation is no longer running',
                key=f'ack_abandoned_{latest["id"]}',
            )
            if st.button('Mark abandoned calculation as interrupted',
                         disabled=not acknowledgement):
                try:
                    journal.interrupt_if_stale(latest['id'])
                    st.rerun()
                except (GuardBlocked, sqlite3.Error, OSError) as exc:
                    st.error(f'Cannot release the abandoned attempt: {exc}')
        else:
            st.caption(f'If this calculation was interrupted, a manual recovery '
                       f'option appears after {RECOVERY_MINUTES} minutes. '
                       'Do not release an attempt while another calculation is running.')
    elif already_attempted and not authorised_repeat:
        state = latest['state']
        requests_started = latest['matrix_count'] + latest['leg_count']
        if state == 'succeeded':
            st.info('This exact calculation has already succeeded. Saved '
                    'results are shown below without making a new Google request.')
        else:
            st.warning(f'The most recent attempt for these inputs ended as '
                       f'**{state}** after recording up to '
                       f'**{requests_started} Google request attempts**. '
                       'An error does not guarantee a request was free. '
                       'A new attempt needs explicit permission.')

    can_calculate = bool(
        journal is not None and depot and route_locations and
        matrix_elements <= MAX_MATRIX_ELEMENTS and not active_attempt and
        (not already_attempted or authorised_repeat)
    )
    if st.button('Calculate optimised route', type='primary', disabled=not can_calculate):
        attempt_id = None
        completed = False
        failure_class = 'CalculationInterrupted'
        try:
            # Validate everything possible *before* reserving or contacting Google.
            minutes(departure)
            if not 0 < float(max_hours) <= 24:
                raise ValueError('Maximum route duration must be between 0 and 24 hours.')
            stops = [{
                'id': 'TXP', 'name': depot['name'],
                'address': depot['address'], 'latitude': depot['latitude'],
                'longitude': depot['longitude'], 'service_minutes': 0,
                'weekly_hours': '{}',
            }]
            stops += route_locations
            stops += [dict(stops[0])]
            for stop in stops:
                try:
                    latitude = float(stop['latitude'])
                    longitude = float(stop['longitude'])
                except (ValueError, TypeError, KeyError) as exc:
                    raise ValueError(f'Invalid coordinates for {stop.get("name", "location")}.') from exc
                if not (math.isfinite(latitude) and math.isfinite(longitude)
                        and -90 <= latitude <= 90 and -180 <= longitude <= 180):
                    raise ValueError(f'Invalid coordinates for {stop["name"]}.')
            if matrix_elements > MAX_MATRIX_ELEMENTS:
                raise ValueError('Route exceeds the Google matrix element limit.')
            departure_at = datetime.combine(
                delivery_date, datetime.strptime(departure, '%H:%M').time(),
                tzinfo=ZoneInfo('Europe/Malta'),
            )
            if traffic and departure_at <= datetime.now(ZoneInfo('Europe/Malta')):
                raise ValueError('Traffic-aware estimates require a future departure date and time.')
            windows = windows_for_date(stops, delivery_date)
            for node, periods in windows.items():
                if not periods:
                    raise OptimisationError(
                        f'{stops[node]["name"]} is closed on the selected delivery date.'
                    )
            if not os.getenv('GOOGLE_MAPS_API_KEY', '').strip():
                raise MapsError('GOOGLE_MAPS_API_KEY is not available in this Codespace.')

            # This is a second, atomic duplicate check against all Streamlit
            # sessions. It runs before the first possible Google call.
            attempt_id = journal.begin(signature, estimate,
                                       allow_repeat=bool(authorised_repeat))
            comparison_info = None
            with st.spinner('Requesting driving times and optimising…'):
                journal.note_request(attempt_id, 'matrix')
                matrix = route_matrix(
                    stops, traffic=traffic,
                    departure_at=departure_at if traffic else None,
                )
                result = optimise(stops, matrix, departure, max_hours, windows)
                # Keep the detailed alternative-route report in the current
                # Streamlit session only, NOT in the durable request journal.
                # It scores all feasible stop permutations with the SAME
                # matrix; this has no additional API usage.
                report = None
                if len(route_locations) <= 7:
                    report = snapshot_report(stops, matrix, departure,
                                             max_hours, windows, top_count=5)

                def charged_traffic_leg(origin, destination, when):
                    journal.note_request(attempt_id, 'leg')
                    return traffic_leg_minutes(origin, destination, when)

                if traffic:
                    if compare_enabled:
                        result, comparison_info = compare_traffic_candidates(
                            stops, matrix, delivery_date, departure, windows,
                            charged_traffic_leg, max_hours=max_hours,
                            max_requests=estimate['leg_requests'],
                            candidate_count=candidate_count,
                        )
                    else:
                        ordered_windows = windows_for_date(result, delivery_date)
                        result = validate_traffic_route(
                            result, delivery_date, departure, ordered_windows,
                            charged_traffic_leg, max_hours,
                        )
                else:
                    # For larger routes the OR-Tools fallback may not provide a
                    # separate drive-time breakdown. Preserve its existing logic.
                    if any('drive_minutes' not in item for item in result):
                        for k, item in enumerate(result):
                            prev = next((i for i, stop in enumerate(stops)
                                         if stop['id'] == result[k-1]['id']), 0) if k else 0
                            dest = next((i for i, stop in enumerate(stops)
                                         if stop['id'] == item['id']), 0)
                            item['drive_minutes'] = 0 if k == 0 else matrix[prev][dest]
                            item['service_minutes'] = (0 if k in (0, len(result)-1)
                                else int(item.get('service_minutes') or 5))
                            item['wait_minutes'] = None

            payload = {
                'route': result, 'traffic': bool(traffic),
                'unverified_names': unverified_names,
                'comparison_info': comparison_info,
            }
            journal.succeed(attempt_id, payload)
            if report is not None:
                st.session_state[f'snapshot_report_{signature}'] = report
            completed = True
            saved = {'payload': payload}
            if unverified_names:
                st.warning('Route calculated, but opening hours remain UNVERIFIED for: '
                           + ', '.join(unverified_names) + '. Schedule is provisional.')
            else:
                st.success('Route calculated and validated. TXP is the start and finish.'
                           if traffic else 'Route optimised. TXP is the start and finish.')
        except (ValueError, MapsError, OptimisationError, GuardBlocked,
                RequestBudgetExceeded, sqlite3.Error, OSError) as exc:
            failure_class = type(exc).__name__
            st.error(str(exc))
        except Exception as exc:
            failure_class = type(exc).__name__
            st.error(f'Unexpected calculation error ({failure_class}). '
                     'The attempt will be retained to prevent a silent retry.')
        finally:
            if attempt_id is not None and not completed:
                # Record even uncertain failures. Do not store provider error
                # strings or secrets in the durable journal.
                try:
                    journal.fail(attempt_id, failure_class)
                except (sqlite3.Error, OSError):
                    st.error('Could not record the failure. The attempt remains '
                             'blocked until its journal state can be inspected.')

    # Render only a successful result for the CURRENT input signature. The
    # journal also restores results after a browser refresh without new charges.
    if saved is not None:
        payload = saved['payload']
        result = payload['route']
        unverified_result = payload.get('unverified_names', [])
        comparison_info = payload.get('comparison_info')
        # Mobile-first result: names and arrival times without a wide table.
        # Stored route data is used directly, no new provider request.
        rows = []
        for ix, stop in enumerate(result):
            clock = escape(str(stop.get('arrival_exact') or stop.get('arrival') or ''))
            label = escape(str(stop.get('name') or 'Unnamed stop'))
            rows.append('<div class="txp-quick-stop">'
                        f'<span class="txp-quick-time">{clock[:5]}</span>'
                        f'<span class="txp-quick-name">{ix + 1}. {label}</span>'
                        '</div>')
        st.markdown('<div class="txp-quick-route">' + ''.join(rows) + '</div>',
                    unsafe_allow_html=True)
        st.link_button('Open full named route in Google Maps',
                       navigation_url(result), use_container_width=True)
        if len(result) - 2 > 3:
            st.caption('Important for iPhone: Google Maps URLs support at most '
                       'three intermediate stops in some mobile browsers. '
                       'If a stop is omitted, use the individual navigation links below.')
        with st.expander('Navigate to each stop (iPhone-safe)', expanded=False):
            st.caption('Tap each stop when ready. Google Maps starts directions '
                       'from your current GPS location. No billable API request.')
            for ix, stop in enumerate(result[1:], start=1):
                title = ('Return to depot' if ix == len(result) - 1
                         else f'{ix}. {stop["name"]}')
                st.link_button(title, navigate_to_stop_url(stop),
                               use_container_width=True)
        if unverified_result:
            st.warning('PROVISIONAL: opening hours not verified for '
                       + ', '.join(unverified_result) + '.')
        if comparison_info is not None:
            st.info(f'Compared {comparison_info["candidates_checked"]} '
                    f'traffic-dependent candidate orders using '
                    f'{comparison_info["google_leg_requests"]} individual '
                    'Google leg queries. Not a global optimum.')
            st.subheader('Traffic comparison results')
            for index, candidate in enumerate(comparison_info['comparisons']):
                status = ('Selected' if index == comparison_info['winner_index']
                          else 'Candidate')
                path = ' → '.join(candidate['order'])
                if candidate['feasible']:
                    st.write(f'**{status} {index + 1}:** {path} | '
                             f'Return {candidate["return_time"]} | '
                             f'{candidate["duration_minutes"]:.3f} min total | '
                             f'{candidate["driving_minutes"]:.3f} min driving')
                else:
                    st.write(f'**Candidate {index + 1}:** {path} | '
                             'Not validated / infeasible')
            saving = comparison_info['minutes_saved_vs_first']
            if saving is None:
                st.warning('First candidate was not validated; minutes saved '
                           'cannot be calculated.')
            else:
                st.success(f'Time saved versus first candidate: {saving:.3f} min.')

        # A complete comparison using the ONE already-purchased snapshot
        # matrix. It is intentionally session-only: never persist raw matrix.
        report = st.session_state.get(f'snapshot_report_{signature}')
        if report is not None:
            with st.expander('Why this route won: same-matrix comparison', expanded=False):
                st.caption(
                    f'Compared {report["evaluated_orders"]} feasible stop orders '
                    'using one consistent Google matrix, full sub-minute '
                    'driving precision, opening hours and unloading. '
                    'Traffic-dependent results may differ at actual journey times.')
                st.write('**Five fastest feasible orders (snapshot)**')
                for i, item in enumerate(report['top_routes']):
                    st.write(f'{i + 1}. {" → ".join(item["order"])} | '
                             f'{item["duration_minutes"]:.3f} min elapsed | '
                             f'{item["driving_minutes"]:.3f} min driving')
                st.write('**Best feasible route for each possible LAST outlet**')
                for item in report['best_by_last']:
                    st.write(f'Last: **{item["last_outlet"]}** | '
                             f'{" → ".join(item["order"])} | '
                             f'{item["duration_minutes"]:.3f} min elapsed')
                st.caption('Matrix digest: ' + (report['matrix_digest'] or 'unavailable') +
                           '. Raw matrix is not saved to the request journal.')
                st.caption('This detailed comparison stays in the current session; '
                           'it is not written to the request journal or exported.')
        else:
            st.caption('Detailed matrix comparison is available in the original '
                       'calculation session only. Opening the saved result does '
                       'not make another billable Google request.')

        with st.expander('Detailed timing table', expanded=False):
            st.dataframe([{
                'Stop': i + 1, 'Location': stop['name'],
                'Arrival': stop['arrival'], 'Departure': stop['departure'],
                'Arrival exact': stop.get('arrival_exact', ''),
                'Departure exact': stop.get('departure_exact', ''),
                'Drive (min)': stop.get('drive_minutes', ''),
                'Wait (min)': (stop.get('wait_minutes') if stop.get('wait_minutes')
                               is not None else '—'),
                'Unload (min)': stop.get('service_minutes', ''),
                'Address': stop.get('address', ''),
            } for i, stop in enumerate(result)], use_container_width=True,
                         hide_index=True)
        if not payload.get('traffic', False):
            st.caption('Traffic-unaware Google driving times (not live traffic). '
                       'Seconds retained while optimising; display values '
                       'are rounded. Opening-hours waiting is reported for '
                       'up to 7 outlets.')
        st.caption('Google Maps • Travel-time data provided by Google Maps Platform '
                   'Routes API. Routes may differ with traffic and departure time.')
        out = io.StringIO()
        fields = ['Stop', 'Location', 'Arrival', 'Departure', 'Address',
                  'Opening hours status']
        writer = csv.DictWriter(out, fieldnames=fields)
        writer.writeheader()
        for i, stop in enumerate(result):
            writer.writerow({
                'Stop': i + 1, 'Location': stop['name'],
                'Arrival': stop['arrival'], 'Departure': stop['departure'],
                'Address': stop.get('address', ''),
                'Opening hours status': ('Unverified - provisional'
                    if stop['name'] in unverified_result
                    else 'No missing hours flagged'),
            })
        st.download_button('Download route CSV', out.getvalue(),
                           'delivery_route.csv', 'text/csv')
elif page == 'Saved Outlets':
    st.subheader(f'Saved delivery outlets ({len(locations)})')
    st.caption('Deleting an outlet here permanently removes its database record. To remove a delivery only, use Delete on Current Route.')
    for loc in locations:
        with st.expander(loc['name']):
            st.caption(f"{loc.get('address') or 'No address'} · {loc.get('service_minutes',5)} min unloading")
            editor(loc,f"edit_{loc['id']}")
            confirm=st.checkbox('Confirm permanent database deletion',key=f"confirm_{loc['id']}")
            if st.button('Permanently delete outlet',key=f"del_{loc['id']}",disabled=not confirm):
                set_active_ids([i for i in active_ids() if i!=loc['id']])
                delete_location(loc['id']);st.rerun()
else:
    st.subheader('Depot / base settings')
    st.caption('Exactly one depot is supported. It is always the route start and finish; it is never an ordinary outlet.')
    current=get_setting('depot_json')
    current=json.loads(current) if current else {'name':'TXP — Ta’ Xbiex Perfumery Ltd.','address':'WF2V+68 Ta’ Xbiex, Malta','latitude':None,'longitude':None}
    st.write('**TXP depot address**')
    st.code(current['address'])
    if st.button('Find / verify TXP on Google Maps'):
        try:
            results=search_place('WF2V+68 Ta’ Xbiex Perfumery Malta')
            st.session_state.depot_matches=results
        except MapsError as exc: st.error(str(exc))
    dep_matches=st.session_state.get('depot_matches',[])
    if dep_matches:
        idx=st.selectbox('Confirm TXP location',range(len(dep_matches)),format_func=lambda i:f"{dep_matches[i]['name']} — {dep_matches[i]['address']}")
        if st.button('Set selected location as the TXP depot'):
            p=dep_matches[idx]; p['name']='TXP — Ta’ Xbiex Perfumery Ltd.'
            save_setting('depot_json',json.dumps(p)); st.success('TXP depot saved.'); st.rerun()
    if current.get('latitude') is not None:
        st.success(f"TXP coordinates: {current['latitude']}, {current['longitude']}")
    st.divider()
    st.subheader('Database and backup')
    st.caption(f'Active trial database: {DB_PATH}')
    st.write(f"Google Maps secret available: {bool(os.getenv('GOOGLE_MAPS_API_KEY'))}")
    buf=io.StringIO(); fields=['name','address','latitude','longitude','weekly_hours','service_minutes','notes','place_id']; w=csv.DictWriter(buf,fieldnames=fields);w.writeheader()
    for row in locations: w.writerow({k:row.get(k) for k in fields})
    st.download_button('Export outlets CSV',buf.getvalue(),'outlets_backup.csv','text/csv')
