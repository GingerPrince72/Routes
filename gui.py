import sqlite3
from datetime import datetime
import tkinter as tk
from tkinter import messagebox, ttk
from ortools.constraint_solver import routing_enums_pb2, pywrapcp

DB_NAME = "delivery_database.db"

def get_all_locations():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, latitude, longitude, open_time, close_time FROM locations")
    rows = cursor.fetchall()
    conn.close()
    return rows

def add_location_to_db(name, lat, lon, open_t, close_t):
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO locations (name, latitude, longitude, open_time, close_time)
            VALUES (?, ?, ?, ?, ?)
        ''', (name, float(lat), float(lon), open_t, close_t))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        messagebox.showerror("Database Error", str(e))
        return False

def run_ortools_optimization(locations_data):
    """Runs OR-Tools VRP with time windows and service durations."""
    num_locations = len(locations_data)
    if num_locations <= 1:
        return "Need at least 2 locations (Depot + Stop) to optimize a route."

    # Simple mock time matrix (in minutes) based on index distance
    # In production, replace this with Haversine distance or Google Distance Matrix API
    time_matrix = []
    for i in range(num_locations):
        row = []
        for j in range(num_locations):
            if i == j:
                row.append(0)
            else:
                row.append(5 + abs(i - j) * 3) # Dummy travel time calculation
        time_matrix.append(row)

    # Service times (10 mins per stop)
    service_times = [0] + [10] * (num_locations - 1)

    # Time windows [earliest, latest] in minutes from day start
    time_windows = [(0, 120)] + [(5, 120)] * (num_locations - 1)

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

    time = 'Time'
    routing.AddDimension(transit_callback_index, 30, 200, False, time)
    time_dimension = routing.GetDimensionOrDie(time)

    for location_idx, time_window in enumerate(data['time_windows']):
        index = manager.NodeToIndex(location_idx)
        time_dimension.CumulVar(index).SetRange(time_window[0], time_window[1])

    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC

    solution = routing.SolveWithParameters(search_parameters)
    if not solution:
        return "No feasible route found satisfying all time windows."

    # Format output
    index = routing.Start(0)
    output_lines = ["Optimized Delivery Route:\n"]
    stop_num = 1
    
    while not routing.IsEnd(index):
        node_index = manager.IndexToNode(index)
        loc_name = locations_data[node_index][1]
        time_var = time_dimension.CumulVar(index)
        output_lines.append(f"{stop_num}. {loc_name} (Arrival Window: {solution.Min(time_var)} - {solution.Max(time_var)} mins)")
        index = solution.Value(routing.NextVar(index))
        stop_num += 1

    # Depot Return
    end_node = manager.IndexToNode(index)
    output_lines.append(f"{stop_num}. Return to {locations_data[end_node][1]}")
    return "\n".join(output_lines)

class RoutePlannerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Route Planner & Location Database")
        self.root.geometry("750x550")
        
        style = ttk.Style()
        style.theme_use("clam")

        title_lbl = ttk.Label(root, text="Route Planner - Location Database", font=("Arial", 16, "bold"))
        title_lbl.pack(pady=10)

        # Table Frame
        table_frame = ttk.Frame(root)
        table_frame.pack(fill=tk.BOTH, expand=True, padx=15, pady=5)

        tree_scroll = ttk.Scrollbar(table_frame)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree = ttk.Treeview(
            table_frame, 
            columns=("ID", "Name", "Latitude", "Longitude", "Open", "Close"), 
            show="headings",
            yscrollcommand=tree_scroll.set
        )
        tree_scroll.config(command=self.tree.yview)

        self.tree.heading("ID", text="ID")
        self.tree.heading("Name", text="Location Name")
        self.tree.heading("Latitude", text="Latitude")
        self.tree.heading("Longitude", text="Longitude")
        self.tree.heading("Open", text="Open Time")
        self.tree.heading("Close", text="Close Time")

        self.tree.column("ID", width=40, anchor=tk.CENTER)
        self.tree.column("Name", width=200, anchor=tk.W)
        self.tree.column("Latitude", width=100, anchor=tk.CENTER)
        self.tree.column("Longitude", width=100, anchor=tk.CENTER)
        self.tree.column("Open", width=80, anchor=tk.CENTER)
        self.tree.column("Close", width=80, anchor=tk.CENTER)

        self.tree.pack(fill=tk.BOTH, expand=True)

        # Action Buttons Frame
        btn_frame = ttk.Frame(root)
        btn_frame.pack(fill=tk.X, padx=15, pady=5)

        btn_optimize = ttk.Button(btn_frame, text="Optimize Route (OR-Tools)", command=self.on_optimize)
        btn_optimize.pack(side=tk.LEFT, padx=5)

        # Control Panel Frame for Adding Locations
        control_frame = ttk.LabelFrame(root, text="Add New Delivery Location", padding=10)
        control_frame.pack(fill=tk.X, padx=15, pady=10)

        ttk.Label(control_frame, text="Name:").grid(row=0, column=0, sticky=tk.W, padx=5, pady=5)
        self.ent_name = ttk.Entry(control_frame, width=20)
        self.ent_name.grid(row=0, column=1, padx=5, pady=5)

        ttk.Label(control_frame, text="Lat:").grid(row=0, column=2, sticky=tk.W, padx=5, pady=5)
        self.ent_lat = ttk.Entry(control_frame, width=10)
        self.ent_lat.grid(row=0, column=3, padx=5, pady=5)

        ttk.Label(control_frame, text="Lon:").grid(row=0, column=4, sticky=tk.W, padx=5, pady=5)
        self.ent_lon = ttk.Entry(control_frame, width=10)
        self.ent_lon.grid(row=0, column=5, padx=5, pady=5)

        ttk.Label(control_frame, text="Open (HH:MM):").grid(row=1, column=0, sticky=tk.W, padx=5, pady=5)
        self.ent_open = ttk.Entry(control_frame, width=10)
        self.ent_open.insert(0, "08:00")
        self.ent_open.grid(row=1, column=1, sticky=tk.W, padx=5, pady=5)

        ttk.Label(control_frame, text="Close (HH:MM):").grid(row=1, column=2, sticky=tk.W, padx=5, pady=5)
        self.ent_close = ttk.Entry(control_frame, width=10)
        self.ent_close.insert(0, "17:00")
        self.ent_close.grid(row=1, column=3, sticky=tk.W, padx=5, pady=5)

        btn_add = ttk.Button(control_frame, text="Add Location", command=self.on_add_location)
        btn_add.grid(row=1, column=5, padx=5, pady=5, sticky=tk.E)

        self.load_data()

    def load_data(self):
        for row in self.tree.get_children():
            self.tree.delete(row)
        rows = get_all_locations()
        for r in rows:
            self.tree.insert("", tk.END, values=r)

    def on_add_location(self):
        name = self.ent_name.get().strip()
        lat = self.ent_lat.get().strip()
        lon = self.ent_lon.get().strip()
        open_t = self.ent_open.get().strip()
        close_t = self.ent_close.get().strip()

        if not name or not lat or not lon:
            messagebox.showwarning("Missing Data", "Please fill in Name, Latitude, and Longitude.")
            return

        if add_location_to_db(name, lat, lon, open_t, close_t):
            messagebox.showinfo("Success", f"Added '{name}' successfully!")
            self.ent_name.delete(0, tk.END)
            self.ent_lat.delete(0, tk.END)
            self.ent_lon.delete(0, tk.END)
            self.load_data()

    def on_optimize(self):
        locations = get_all_locations()
        result = run_ortools_optimization(locations)
        messagebox.showinfo("Optimized Route Result", result)

if __name__ == "__main__":
    root = tk.Tk()
    app = RoutePlannerApp(root)
    root.mainloop()