import sqlite3
from datetime import datetime
import streamlit as st
import requests
from ortools.constraint_solver import routing_enums_pb2, pywrapcp

DB_NAME = "delivery_database.db"
# Replace with your Google Maps API key (requires Routes API enabled in Google Cloud)
GOOGLE_MAPS_API_KEY = st.secrets.get("GOOGLE_MAPS_API_KEY", "AIzaSyAdC7kpz8ftCqVjSLblO5klOjX_-1fHkQ8")
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
            close_time TEXT NOT NULL
        )
    ''')
    conn.commit()
    conn.close()

def get_all_locations():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, latitude, longitude, open_time, close_time FROM locations")
    rows = cursor.fetchall()
    conn.close()
    return rows

def add_location(name, lat, lon, open_t, close_t):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO locations (name, latitude, longitude, open_time, close_time)
        VALUES (?, ?, ?, ?, ?)
    ''', (name, float(lat), float(lon), open_t, close_t))
    conn.commit()
    conn.close()

def delete_location(loc_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM locations WHERE id = ?", (loc_id,))
    conn.commit()
    conn.close()

def time_to_minutes(time_str):
    """Converts 'HH:MM' string to total minutes from midnight"""
    try:
        t = datetime.strptime(time_str.strip(), "%H:%M").time()
        return t.hour * 60 + t.minute
    except:
        return 0

def fetch_routes_api_matrix(locations):
    """Fetches travel times using Google's Routes API (Compute Route Matrix) with live traffic."""
    num_locs = len(locations)
    matrix = [[10] * num_locs for _ in range(num_locs)]

    if not GOOGLE_MAPS_API_KEY or GOOGLE_MAPS_API_KEY == "YOUR_GOOGLE_MAPS_API_KEY":
        # Fallback synthetic matrix if no API key is provided yet
        for i in range(num_locs):
            for j in range(num_locs):
                if i == j:
                    matrix[i][j] = 0
                else:
                    matrix[i][j] = 5 + abs(i - j) * 3
        return matrix

    url = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
    
    waypoints = [
        {"placeId": None, "location": {"latLng": {"latitude": loc[2], "longitude": loc[3]}}}
        for loc in locations
    ]
    
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
                # Parse strings like "120s" or "305.5s" into minutes
                duration_sec = float(duration_str.replace("s", ""))
                matrix[orig_idx][dest_idx] = round(duration_sec / 60)
    except Exception as e:
        st.error(f"Routes API Error: {str(e)}")

    return matrix

def run_optimization(locations_data):
    num_locations = len(locations_data)
    if num_locations <= 1:
        return "Error: Need at least 2 locations (Depot + Stop)."

    time_matrix = fetch_routes_api_matrix(locations_data)
    service_times = [0] + [10] * (num_locations - 1)  # 10 mins service duration per stop

    # Build time windows in minutes from midnight based on database hours
    time_windows = []
    for idx, loc in enumerate(locations_data):
        open_min = time_to_minutes(loc[4])
        close_min = time_to_minutes(loc[5])
        if idx == 0:
            time_windows.append((420, 1440))  # Depot available from 07:00 AM
        else:
            time_windows.append((open_min, close_min))

    data = {
        'time_matrix': time_matrix,
        'service_times': service_times,
        'time_windows': time_windows,
        'num_vehicles': 1,
        'depot': 0
    }

    manager = pywrapcp.RoutingIndexManager(len(data['time_matrix']), data['num_vehicles'], data['depot'])
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

    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC

    solution = routing.SolveWithParameters(search_parameters)
    if not solution:
        return "No feasible route found satisfying outlet opening hours and time windows."

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
            "close": loc[5]
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
                st.markdown(f"- **{r[1]}** (Lat: {r[2]}, Lon: {r[3]}) | *Hours: {r[4]} - {r[5]}*")
        else:
            st.info("No delivery locations saved yet.")

    with tab2:
    st.subheader("Add New Delivery Stop via Google Maps")
    search_query = st.text_input("Search Location on Google Maps", placeholder="e.g., Xerri Bakery Malta or Ta' Xbiex Pharmacy")
    
    if search_query:
        if not GOOGLE_MAPS_API_KEY or GOOGLE_MAPS_API_KEY == "YOUR_GOOGLE_MAPS_API_KEY":
            st.error("Google Maps API key is required for location search.")
        else:
            # Call Google Places API (Text Search v1)
            places_url = "https://places.googleapis.com/v1/places:searchText"
            headers = {
                "Content-Type": "application/json",
                "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
                "X-Goog-FieldMask": "places.displayName,places.location,places.formattedAddress"
            }
            payload = {"textQuery": search_query}
            
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
                    
                    st.write(f"**Coordinates:** {lat}, {lon}")
                    
                    col1, col2 = st.columns(2)
                    with col1:
                        open_t = st.text_input("Opening Time (HH:MM)", "08:00", key="map_open")
                    with col2:
                        close_t = st.text_input("Closing Time (HH:MM)", "17:00", key="map_close")
                        
                    if st.button("Save Selected Location"):
                        add_location(name, lat, lon, open_t, close_t)
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
        if st.button("Calculate Optimized Route", type="primary"):
            locations = get_all_locations()
            if len(locations) < 2:
                st.warning("You need at least 2 locations in your database to optimize.")
            else:
                with st.spinner("Computing optimal route via Google Routes API & time windows..."):
                    route_steps = run_optimization(locations)
                    if isinstance(route_steps, str):
                        st.error(route_steps)
                    else:
                        st.success("Route optimized successfully!")
                        
                        origin = f"{route_steps[0]['lat']},{route_steps[0]['lon']}"
                        destination = f"{route_steps[-1]['lat']},{route_steps[-1]['lon']}"
                        waypoints = "|".join([f"{s['lat']},{s['lon']}" for s in route_steps[1:-1]])
                        
                        maps_export_url = f"https://www.google.com/maps/dir/?api=1&origin={origin}&destination={destination}"
                        if waypoints:
                            maps_export_url += f"&waypoints={waypoints}"
                        
                        st.markdown(
                            f"### 🗺️ [Export Full Route to Google Maps App]({maps_export_url})",
                            unsafe_allow_html=True
                        )
                        st.divider()

                        for step in route_steps:
                            stop_maps_url = f"https://www.google.com/maps/search/?api=1&query={step['lat']},{step['lon']}"
                            st.markdown(
                                f"**Stop {step['stop_num']}: {step['name']}**<br>"
                                f"🕒 *Estimated Arrival: {step['arrival_time']}* (Outlet Open: {step['open']} - {step['close']})<br>"
                                f"📍 [Open Stop in Maps]({stop_maps_url})",
                                unsafe_allow_html=True
                            )
                            st.divider()

if __name__ == "__main__":
    main()