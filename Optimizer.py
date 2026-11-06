from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp

def create_data_model():
    data = {}
    # Time matrix in minutes from location i to j
    data['time_matrix'] = [
        [0, 6, 9, 8, 7],
        [6, 0, 8, 3, 2],
        [9, 8, 0, 5, 6],
        [8, 3, 5, 0, 4],
        [7, 2, 6, 4, 0],
    ]
    
    # NEW: Service / unload duration at each location (in minutes)
    data['service_times'] = [
        0,   # Depot (Start/End - no unload time)
        10,  # Stop 1 unload duration
        15,  # Stop 2 unload duration
        10,  # Stop 3 unload duration
        20,  # Stop 4 unload duration
    ]

    # Time windows for each location [earliest_arrival, latest_arrival] in minutes from 0
    data['time_windows'] = [
        (0, 5),   # Depot
        (5, 20),  # Stop 1
        (10, 30), # Stop 2
        (5, 20),  # Stop 3
        (15, 35), # Stop 4
    ]
    data['num_vehicles'] = 1
    data['depot'] = 0
    return data

def main():
    data = create_data_model()

    manager = pywrapcp.RoutingIndexManager(
        len(data['time_matrix']), data['num_vehicles'], data['depot']
    )
    routing = pywrapcp.RoutingModel(manager)

    # UPDATED: Transit callback now includes travel time + service time at the departure node
    def time_callback(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        # Travel time from 'from_node' to 'to_node' + service time spent at 'from_node'
        return data['time_matrix'][from_node][to_node] + data['service_times'][from_node]

    transit_callback_index = routing.RegisterTransitCallback(time_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)

    # Add Time Dimension
    time = 'Time'
    routing.AddDimension(
        transit_callback_index,
        30,   # allow waiting time (slack) up to 30 minutes if arriving early
        100,  # maximum total time per vehicle route
        False, 
        time,
    )
    time_dimension = routing.GetDimensionOrDie(time)

    # Add Time Window constraints for every location
    for location_idx, time_window in enumerate(data['time_windows']):
        index = manager.NodeToIndex(location_idx)
        time_dimension.CumulVar(index).SetRange(time_window[0], time_window[1])

    for i in range(data['num_vehicles']):
        routing.AddVariableMinimizedByFinalizer(
            time_dimension.CumulVar(routing.Start(i))
        )
        routing.AddVariableMinimizedByFinalizer(
            time_dimension.CumulVar(routing.End(i))
        )

    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    )

    solution = routing.SolveWithParameters(search_parameters)

    if solution:
        print_solution(data, manager, routing, solution)
    else:
        print("No solution found with these service times and time windows!")

def print_solution(data, manager, routing, solution):
    time_dimension = routing.GetDimensionOrDie('Time')
    for vehicle_id in range(data['num_vehicles']):
        index = routing.Start(vehicle_id)
        plan_output = f'Route for vehicle {vehicle_id}:\n'
        while not routing.IsEnd(index):
            node_index = manager.IndexToNode(index)
            time_var = time_dimension.CumulVar(index)
            plan_output += (
                f'Stop {node_index} (Service: {data["service_times"][node_index]} min) '
                f'-> Arrival Window: [{solution.Min(time_var)}, {solution.Max(time_var)}]\n'
            )
            index = solution.Value(routing.NextVar(index))
        node_index = manager.IndexToNode(index)
        time_var = time_dimension.CumulVar(index)
        plan_output += f'Stop {node_index} (Depot Return) -> Arrival: [{solution.Min(time_var)}, {solution.Max(time_var)}]\n'
        print(plan_output)

if __name__ == '__main__':
    main()