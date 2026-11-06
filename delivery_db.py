import sqlite3
from datetime import datetime

def initialize_database():
    """Creates a SQLite database and tables for locations and opening hours."""
    conn = sqlite3.connect('delivery_database.db')
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

def seed_sample_data():
    """Populates the database with delivery locations and operating hours."""
    conn = sqlite3.connect('delivery_database.db')
    cursor = conn.cursor()
    
    stops = [
        ("Pharmacy 1 (Gzira)", 35.9081, 14.4984, "08:00", "13:00"),
        ("Retail Shop (Ta' Xbiex)", 35.9042, 14.4912, "09:00", "17:00"),
        ("Clinic (Msida)", 35.9123, 14.4855, "08:30", "15:30"),
        ("Supermarket (Hamrun)", 35.8989, 14.5144, "07:30", "19:00"),
        ("Pharmacy 2 (Valletta)", 35.9150, 14.5021, "09:00", "13:30")
    ]
    
    cursor.executemany('''
        INSERT OR IGNORE INTO locations (name, latitude, longitude, open_time, close_time)
        VALUES (?, ?, ?, ?, ?)
    ''', stops)
    
    conn.commit()
    conn.close()

def get_open_locations(current_time_str):
    """Returns only the locations that are open at the specified time string ('HH:MM')."""
    conn = sqlite3.connect('delivery_database.db')
    cursor = conn.cursor()
    
    current_time = datetime.strptime(current_time_str, "%H:%M").time()
    
    cursor.execute('SELECT name, latitude, longitude, open_time, close_time FROM locations')
    all_locations = cursor.fetchall()
    conn.close()
    
    valid_stops = []
    for loc in all_locations:
        name, lat, lon, open_t, close_t = loc
        o_time = datetime.strptime(open_t, "%H:%M").time()
        c_time = datetime.strptime(close_t, "%H:%M").time()
        
        if o_time <= current_time <= c_time:
            valid_stops.append({
                "name": name,
                "coordinates": (lat, lon),
                "open_time": open_t,
                "close_time": close_t
            })
            
    return valid_stops

if __name__ == '__main__':
    initialize_database()
    seed_sample_data()
    
    test_time = "10:30"
    print(f"--- Delivery Stops Open at {test_time} ---")
    open_stops = get_open_locations(test_time)
    for stop in open_stops:
        print(f"✓ {stop['name']} (Open: {stop['open_time']} - {stop['close_time']})")