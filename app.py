import sqlite3
import streamlit as st
import datetime
import requests
import json

DB_NAME = "routes.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            latitude REAL,
            longitude REAL,
            open_time TEXT,
            close_time TEXT,
            is_depot INTEGER DEFAULT 0
        )
    """)
    # Automatically check and add missing column if database already existed
    cursor.execute("PRAGMA table_info(locations)")
    columns = [col[1] for col in cursor.fetchall()]
    if "is_depot" not in columns:
        cursor.execute("ALTER TABLE locations ADD COLUMN is_depot INTEGER DEFAULT 0")
    conn.commit()
    conn.close()

def get_all_locations():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, latitude, longitude, open_time, close_time, is_depot FROM locations")
    rows = cursor.fetchall()
    conn.close()
    return rows

def add_location(name, latitude, longitude, open_time, close_time, is_depot):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO locations (name, latitude, longitude, open_time, close_time, is_depot)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (name, latitude, longitude, open_time, close_time, is_depot))
    conn.commit()
    conn.close()

def delete_location(loc_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM locations WHERE id = ?", (loc_id,))
    conn.commit()
    conn.close()

def main():
    init_db()

    st.title("🚚 Mobile Route Planner")
    st.write("Database, Outlet Hours, Google Routes API & Maps Export")

    tab1, tab2, tab3, tab4 = st.tabs(["📋 View Locations", "➕ Add Stop", "🗺️ Route Optimizer", "⚙️ Settings"])

    with tab1:
        st.subheader("Saved Delivery Locations")
        rows = get_all_locations()
        if rows:
            for r in rows:
                loc_id, name, lat, lon, open_t, close_t, is_depot = r
                badge = " 🏢 **[START / DEPOT]**" if is_depot == 1 else ""
                st.markdown(f"**{name}**{badge}")
                st.text(f"Coords: {lat}, {lon} | Hours: {open_t} - {close_t}")
                if st.button("Delete", key=f"del_{loc_id}"):
                    delete_location(loc_id)
                    st.rerun()
                st.divider()
        else:
            st.info("No delivery locations saved yet. Add some in the 'Add Stop' tab!")

    with tab2:
        st.subheader("Add New Delivery Stop")
        with st.form("add_stop_form"):
            name = st.text_input("Outlet / Location Name")
            latitude = st.number_input("Latitude", value=35.90, format="%.6f")
            longitude = st.number_input("Longitude", value=14.50, format="%.6f")
            open_time = st.text_input("Opening Time (HH:MM)", value="08:00")
            close_time = st.text_input("Closing Time (HH:MM)", value="17:00")
            is_depot = st.checkbox("Mark as Start / Depot location")
            
            submitted = st.form_submit_button("Save Location")
            if submitted:
                if name.strip():
                    add_location(name, latitude, longitude, open_time, close_t if 'close_t' in locals() else close_time, 1 if is_depot else 0)
                    st.success(f"Added {name} successfully!")
                    st.rerun()
                else:
                    st.error("Please enter a location name.")

    with tab3:
        st.subheader("Route Optimizer & Maps Export")
        rows = get_all_locations()
        if rows:
            st.write(f"Ready to optimize route for {len(rows)} locations.")
            if st.button("Generate Google Maps Navigation Link"):
                coords = [f"{r[2]},{r[3]}" for r in rows]
                maps_export_url = f"https://www.google.com/maps/dir/?api=1&destination={coords[-1]}"
                if len(coords) > 2:
                    waypoints = "|".join(coords[1:-1])
                    maps_export_url += f"&waypoints={waypoints}"
                st.markdown(f"[Click here to open optimized route in Google Maps]({maps_export_url})", unsafe_allow_html=True)
        else:
            st.warning("Add locations first to generate routes.")

    with tab4:
        st.subheader("Settings & Database Management")
        if st.button("Reset / Clear Database"):
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()
            cursor.execute("DROP TABLE IF EXISTS locations")
            conn.commit()
            conn.close()
            init_db()
            st.success("Database reset successfully!")
            st.rerun()

if __name__ == "__main__":
    main()