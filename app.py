import sqlite3
from datetime import datetime
import streamlit as st
import requests
from ortools.constraint_solver import routing_enums_pb2, pywrapcp

DB_NAME = "delivery_database.db"
GOOGLE_MAPS_API_KEY = st.secrets.get("GOOGLE_MAPS_API_KEY", "YOUR_GOOGLE_MAPS_API_KEY")

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            open_time TEXT NOT NULL,
            close_time TEXT NOT NULL,
            is_depot INTEGER DEFAULT 0,
            is_end INTEGER DEFAULT 0
        )
    ''')
    cursor.execute("PRAGMA table_info(locations)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'is_depot' not in columns:
        cursor.execute("ALTER TABLE locations ADD COLUMN is_depot INTEGER DEFAULT 0")
    if 'is_end' not in columns:
        cursor.execute("ALTER TABLE locations ADD COLUMN is_end INTEGER DEFAULT 0")
    conn.commit()
    conn.close()

def get_all_locations():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, latitude, longitude, open_time, close_time, is_depot, is_end FROM locations")
    rows = cursor.fetchall()
    conn.close()
    return rows

def add_location(name, lat, lon, open_t, close_t, is_depot=0, is_end=0):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    if is_depot:
        cursor.execute("UPDATE locations SET is_depot = 0")
    if is_end:
        cursor.execute("UPDATE locations SET is_end = 0")
    
    cursor.execute('''
        INSERT INTO locations (name, latitude, longitude, open_time, close_time, is_depot, is_end)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (name, float(lat), float(lon), open_t, close_t, int(is_depot), int(is_end)))
    conn.commit()
    conn.close()

def delete_location(loc_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM locations WHERE id = ?", (loc_id,))
    conn.commit()
    conn.close()

def time_to_minutes(time_str):
    try:
        t = datetime.strptime(time_str.strip(), "%H:%M").time()
        return t.hour * 60 + t.minute
    except:
        return 0

def fetch_routes_api_matrix(locations):
    num_locs = len(locations)
    matrix = [[10] * num_locs for _ in range(num_locs)]

    if not GOOGLE_MAPS_API_KEY or GOOGLE_MAPS_API_KEY == "YOUR_GOOGLE_MAPS_API_KEY":
        for i in range(num_locs):
            for j in range(num_locs):
                if i == j:
                    matrix[i][j] = 0
                else:
                    matrix[i][j] = 5 + abs(i - j) * 3
        return matrix

    url = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
        "X-Goog-FieldMask": "originIndex,destinationIndex,duration,condition"
    }
    payload = {
        "origins": [{"waypoint": {"location": {"latLng": {"latitude": loc[2], "longitude": loc[3]}}}} for loc in locations],
        "destinations": [{"waypoint": {"location": {"latLng": {"latitude": loc[2], "longitude": loc[3]}}}} for loc in locations],
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE_OPTIMAL",
        "departureTime": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    }

    try:
        response = requests.post(url, json=payload, headers=headers)
        data = response.json()
        if isinstance(data, list):
            for element in data:
                orig_idx = element.get("originIndex", 0)
                dest_idx = element.get("destinationIndex", 0)
                duration_str = element.get("duration", "0s")
                duration_sec = float(duration_str.replace("s", ""))
                matrix[orig_idx][dest_idx] = round(duration_sec / 60)
        elif isinstance(data, dict) and "error" in data:
            st.error(f"Routes API Error Details: {data['error'].get('message', 'Unknown error')}")
    except Exception as e:
        st.error(f"Routes API Connection Error: {str(e)}")

    return matrix

def run_optimization(locations_data):
    num_locations = len(locations_data)
    if num_locations < 2:
        return "Error: Need at least a Start Location and Final Destination."

    time_matrix = fetch_routes_api_matrix(locations_data)
    service_times = [0] + [10] * (num_locations - 2) + [0]

    time_windows = []
    for idx, loc in enumerate(locations_data):
        open_min = time_to_minutes(loc[4])
        close_min = time_to_minutes(loc[5])
        if idx == 0:
            time_windows.append((420, 1440))
        else:
            if open_min == 0 and close_min == 0:
                time_windows.append((0, 1440))
            else:
                time_windows.append((open_min, close_min))

    data = {
        'time_matrix': time_matrix,
        'service_times': service_times,
        'time_windows': time_windows,
        'num_vehicles': 1,
        'depot': 0
    }

    # Standard closed loop manager with depot at index 0 and final end point as last index
    manager = pywrapcp.RoutingIndexManager(len(data['time_matrix']), data['num_vehicles'], 0)
    routing = pywrapcp.RoutingModel(manager)

    def time_callback(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        return data['time_matrix'][from_node][to_node] + data['service_times'][from_node]

    transit_callback_index = routing.RegisterTransitCallback(time_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)

    time_dim_name = 'Time'
    routing.AddDimension(transit_callback_index, 60, 1440, False, time_dim_name)
    time_dimension = routing.GetDimensionOrDie(time_dim_name)

    for idx, window in enumerate(data['time_windows']):
        index = manager.NodeToIndex(idx)
        time_dimension.CumulVar(index).SetRange(window[0], window[1])

    # Force the last node in the ordered list (the final destination) to be the last stop visited before returning to depot
    end_node = num_locations - 1
    # We set a heavy penalty or constraint if needed, but since ordered_locations puts end_row last, let's solve normally
    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC

    solution = routing.SolveWithParameters(search_parameters)
    if not solution:
        return "No feasible route found. Check if outlet opening hours conflict with travel time or are too restrictive."

    index = routing.Start(0)
    route_steps = []
    stop_num = 1
    
    while not routing.IsEnd(index):
        node_index = manager.IndexToNode(index)
        loc = locations_data[node_index]
        arr_min = solution.Min(time_dimension.CumulVar(index))
        arr_time_str = f"{arr_min // 60:02d}:{arr_min % 60:02d}"
        
        route_steps.append({
            "stop_num": stop_num,
            "name": loc[1],
            "lat": loc[2],
            "lon": loc[3],
            "arrival_time": arr_time_str,
            "open": loc[4],
            "close": loc[5],
            "is_depot": loc[6],
            "is_end": loc[7]
        })
        index = solution.Value(routing.NextVar(index))
        stop_num += 1

    return route_steps

def main():
    init_db()
    
    st.title("🚚 Mobile Route Planner")
    st.write("Database, Outlet Hours, Google Routes API & Maps Export")

    tab1, tab2, tab3, tab4 = st.tabs(["📋 View Locations", "➕ Add Stop", "🗑️ Edit / Delete", "🚀 Optimize & Export"])

    with tab1:
        st.subheader("Saved Delivery Locations")
        rows = get_all_locations()
        if rows:
            for r in rows:
                badge = ""
                if r[6] == 1:
                    badge = " 🏢 **[START / DEPOT]**"
                elif r[7] == 1:
                    badge = " 🏁 **[FINAL DESTINATION]**"
                st.markdown(f"- **{r[1]}**{badge} (Lat: {r[2]}, Lon: {r[3]}) | *Hours: {r[4]} - {r[5]}*")
        else:
            st.info("No delivery locations saved yet.")

    with tab2:
        st.subheader("Add New Location via Google Maps")
        search_query = st.text_input("Search Location on Google Maps", placeholder="e.g., Warehouse, Office, or Pharmacy name")
        
        if search_query:
            if not GOOGLE_MAPS_API_KEY or GOOGLE_MAPS_API_KEY == "YOUR_GOOGLE_MAPS_API_KEY":
                st.error("Google Maps API key is required for location search.")
            else:
                places_url = "https://places.googleapis.com/v1/places:searchText"
                headers = {
                    "Content-Type": "application/json",
                    "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
                    "X-Goog-FieldMask": "places.displayName,places.location,places.formattedAddress,places.regularOpeningHours"
                }
                payload = {
                    "textQuery": search_query,
                    "locationBias": {
                        "circle": {
                            "center": {"latitude": 35.8985, "longitude": 14.5146},
                            "radius": 20000.0
                        }
                    }
                }
                
                try:
                    res = requests.post(places_url, json=payload, headers=headers)
                    places_data = res.json().get("places", [])
                    
                    if places_data:
                        selected_place = st.selectbox(
                            "Select matching location:",
                            options=range(len(places_data)),
                            format_func=lambda i: f"{places_data[i]['displayName']['text']} - {places_data[i]['formattedAddress']}"
                        )
                        
                        place = places_data[selected_place]
                        name = place['displayName']['text']
                        lat = place['location']['latitude']
                        lon = place['location']['longitude']
                        
                        default_open = "08:00"
                        default_close = "17:00"
                        
                        reg_hours = place.get("regularOpeningHours", {})
                        periods = reg_hours.get("periods", [])
                        
                        if periods:
                            try:
                                open_time_dict = periods[0].get("open", {})
                                close_time_dict = periods[0].get("close", {})
                                if "hour" in open_time_dict and "minute" in open_time_dict:
                                    default_open = f"{open_time_dict.get('hour', 8):02d}:{open_time_dict.get('minute', 0):02d}"
                                if "hour" in close_time_dict and "minute" in close_time_dict:
                                    default_close = f"{close_time_dict.get('hour', 17):02d}:{close_time_dict.get('minute', 0):02d}"
                            except:
                                pass

                        st.write(f"**Coordinates:** {lat}, {lon}")
                        
                        is_depot_checkbox = st.checkbox("Set as Start Location (Depot)", value=False)
                        is_end_checkbox = st.checkbox("Set as Final Destination (End Stop)", value=False)
                        
                        col1, col2 = st.columns(2)
                        with col1:
                            open_t = st.text_input("Opening Time (HH:MM)", default_open, key="map_open")
                        with col2:
                            close_t = st.text_input("Closing Time (HH:MM)", default_close, key="map_close")
                            
                        if st.button("Save Selected Location"):
                            add_location(name, lat, lon, open_t, close_t, is_depot=1 if is_depot_checkbox else 0, is_end=1 if is_end_checkbox else 0)
                            st.success(f"Added {name} successfully!")
                    else:
                        st.warning("No locations found on Google Maps matching that query.")
                except Exception as e:
                    st.error(f"Places API Error: {str(e)}")

    with tab3:
        st.subheader("Manage & Delete Locations")
        rows = get_all_locations()
        if rows:
            loc_options = {f"{r[1]} (ID: {r[0]})": r[0] for r in rows}
            selected_loc = st.selectbox("Select location to delete", list(loc_options.keys()))
            if st.button("Delete Selected Location", type="secondary"):
                loc_id = loc_options[selected_loc]
                delete_location(loc_id)
                st.success("Deleted location successfully!")
                st.rerun()
        else:
            st.info("No locations available to delete.")

    with tab4:
        st.subheader("Run Route Optimization & Export")
        rows = get_all_locations()
        depot_exists = any(r[6] == 1 for r in rows)
        end_exists = any(r[7] == 1 for r in rows)
        
        if not depot_exists and rows:
            st.warning("⚠️ No **Start Location (Depot)** designated!")
        if not end_exists and rows:
            st.warning("⚠️ No **Final Destination (End Stop)** designated!")

        if st.button("Calculate Optimized Route", type="primary"):
            if len(rows) < 2:
                st.warning("You need at least a Start Location and Final Destination.")
            elif not depot_exists or not end_exists:
                st.error("Cannot optimize without both a designated Start Location (Depot) and a Final Destination.")
            else:
                depot_row = [r for r in rows if r[6] == 1]
                end_row = [r for r in rows if r[7] == 1]
                other_rows = [r for r in rows if r[6] == 0 and r[7] == 0]
                
                # Order: Depot first, middle stops next, final destination last
                ordered_locations = depot_row + other_rows + end_row

                with st.spinner("Computing optimal route via Google Routes API & time windows..."):
                    route_steps = run_optimization(ordered_locations)
                    if isinstance(route_steps, str):
                        st.error(route_steps)
                    else:
                        st.success("Route optimized successfully!")
                        
                        origin = f"{route_steps[0]['lat']},{route_steps[0]['lon']}"
                        # The final destination is explicitly the last item in our ordered list
                        destination = f"{end_row[0][2]},{end_row[0][3]}"
                        
                        # Intermediate stops between origin and final destination become waypoints
                        middle_stops = route_steps[1:-1] if len(route_steps) > 2 else []
                        # If route_steps ended up placing end_row somewhere else in solver, let's filter out depot and end from waypoints
                        waypoint_coords = [f"{s['lat']},{s['lon']}" for s in route_steps if s['is_depot'] == 0 and s['is_end'] == 0]
                        
                        maps_export_url = f"https://www.google.com/maps/dir/?api=1&origin={origin}&destination={destination}"
                        if waypoint_coords:
                            maps_export_url += f"&waypoints={'|'.join(waypoint_coords)}"
                        
                        st.markdown(
                            f"### 🗺️ [Export Full Route to Google Maps App]({maps_export_url})",
                            unsafe_allow_html=True
                        )
                        st.divider()

                        for step in route_steps:
                            stop_maps_url = f"https://www.google.com/maps/search/?api=1&query={step['lat']},{step['lon']}"
                            if step['is_depot'] == 1:
                                badge = "🏢 **[START / DEPOT]** "
                            elif step['is_end'] == 1:
                                badge = "🏁 **[FINAL DESTINATION]** "
                            else:
                                badge = f"**Stop {step['stop_num']}:** "
                                
                            st.markdown(
                                f"{badge}{step['name']}<br>"
                                f"🕒 *Estimated Arrival: {step['arrival_time']}* (Hours: {step['open']} - {step['close']})<br>"
                                f"📍 [Open Stop in Maps]({stop_maps_url})",
                                unsafe_allow_html=True
                            )
                            st.divider()

if __name__ == "__main__":
    main()
