import math


EARTH_RADIUS_KM = 6371.0088


def haversine_distance_km(
    lat1,
    lon1,
    lat2,
    lon2,
):
    """
    Calculate great-circle distance between two
    latitude/longitude points.
    """

    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)

    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1_rad)
        * math.cos(lat2_rad)
        * math.sin(delta_lon / 2) ** 2
    )

    c = 2 * math.atan2(
        math.sqrt(a),
        math.sqrt(1 - a),
    )

    return EARTH_RADIUS_KM * c


def project_point(
    latitude,
    longitude,
    reference_latitude,
):
    """
    Convert latitude/longitude into an approximate
    local metric coordinate system.

    Good enough for route-corridor filtering over
    regional distances.
    """

    lat_rad = math.radians(reference_latitude)

    x = (
        math.radians(longitude)
        * EARTH_RADIUS_KM
        * math.cos(lat_rad)
    )

    y = (
        math.radians(latitude)
        * EARTH_RADIUS_KM
    )

    return x, y


def point_to_segment_distance(
    px,
    py,
    ax,
    ay,
    bx,
    by,
):
    """
    Calculate distance from point P to line segment AB.
    """

    dx = bx - ax
    dy = by - ay

    if dx == 0 and dy == 0:
        return math.sqrt(
            (px - ax) ** 2
            + (py - ay) ** 2
        )

    t = (
        (px - ax) * dx
        + (py - ay) * dy
    ) / (
        dx * dx + dy * dy
    )

    t = max(
        0,
        min(1, t),
    )

    closest_x = ax + t * dx
    closest_y = ay + t * dy

    return math.sqrt(
        (px - closest_x) ** 2
        + (py - closest_y) ** 2
    )


def calculate_distance_to_route_km(
    route_coordinates,
    latitude,
    longitude,
):
    """
    Calculate the minimum approximate distance
    between a charger and the OSRM route.
    """

    if not route_coordinates:
        return None

    reference_latitude = (
        route_coordinates[0][1]
    )

    charger_x, charger_y = project_point(
        latitude,
        longitude,
        reference_latitude,
    )

    minimum_distance = float("inf")

    for i in range(
        len(route_coordinates) - 1
    ):

        lon1, lat1 = route_coordinates[i]
        lon2, lat2 = route_coordinates[i + 1]

        ax, ay = project_point(
            lat1,
            lon1,
            reference_latitude,
        )

        bx, by = project_point(
            lat2,
            lon2,
            reference_latitude,
        )

        distance = point_to_segment_distance(
            charger_x,
            charger_y,
            ax,
            ay,
            bx,
            by,
        )

        minimum_distance = min(
            minimum_distance,
            distance,
        )

    return round(
        minimum_distance,
        3,
    )


def calculate_route_position_percent(
    route_coordinates,
    latitude,
    longitude,
):
    """
    Estimate where the charger lies along the route.

    Returns a value from 0 to 100.
    """

    if len(route_coordinates) < 2:
        return None

    reference_latitude = (
        route_coordinates[0][1]
    )

    charger_x, charger_y = project_point(
        latitude,
        longitude,
        reference_latitude,
    )

    best_distance = float("inf")
    best_position = 0.0
    cumulative_distance = 0.0

    segment_lengths = []

    for i in range(
        len(route_coordinates) - 1
    ):

        lon1, lat1 = route_coordinates[i]
        lon2, lat2 = route_coordinates[i + 1]

        segment_length = haversine_distance_km(
            lat1,
            lon1,
            lat2,
            lon2,
        )

        segment_lengths.append(
            segment_length
        )

    total_route_distance = sum(
        segment_lengths
    )

    for i in range(
        len(route_coordinates) - 1
    ):

        lon1, lat1 = route_coordinates[i]
        lon2, lat2 = route_coordinates[i + 1]

        ax, ay = project_point(
            lat1,
            lon1,
            reference_latitude,
        )

        bx, by = project_point(
            lat2,
            lon2,
            reference_latitude,
        )

        dx = bx - ax
        dy = by - ay

        if dx == 0 and dy == 0:
            t = 0
        else:
            t = (
                (charger_x - ax) * dx
                + (charger_y - ay) * dy
            ) / (
                dx * dx + dy * dy
            )

            t = max(
                0,
                min(1, t),
            )

        closest_x = ax + t * dx
        closest_y = ay + t * dy

        distance = math.sqrt(
            (charger_x - closest_x) ** 2
            + (charger_y - closest_y) ** 2
        )

        if distance < best_distance:

            best_distance = distance

            best_position = (
                cumulative_distance
                + segment_lengths[i] * t
            )

        cumulative_distance += (
            segment_lengths[i]
        )

    if total_route_distance <= 0:
        return 0.0

    return round(
        (
            best_position
            / total_route_distance
        ) * 100,
        2,
    )


def filter_chargers_near_route(
    route_geometry,
    stations,
    corridor_km=5.0,
):
    """
    Return only charging stations within the
    configured corridor of the actual OSRM route.

    corridor_km:
        Maximum distance from the route for a charger
        to be considered route-relevant.
    """

    route_coordinates = route_geometry.get(
        "coordinates",
        [],
    )

    if len(route_coordinates) < 2:
        raise ValueError(
            "Route geometry must contain at least "
            "two coordinates."
        )

    route_chargers = []

    for station in stations:

        latitude = station.get(
            "latitude"
        )

        longitude = station.get(
            "longitude"
        )

        if latitude is None or longitude is None:
            continue

        try:
            latitude = float(latitude)
            longitude = float(longitude)

        except (
            TypeError,
            ValueError,
        ):
            continue

        distance_from_route = (
            calculate_distance_to_route_km(
                route_coordinates,
                latitude,
                longitude,
            )
        )

        if (
            distance_from_route is None
            or distance_from_route > corridor_km
        ):
            continue

        route_position = (
            calculate_route_position_percent(
                route_coordinates,
                latitude,
                longitude,
            )
        )

        charger = dict(station)

        charger[
            "distance_from_route_km"
        ] = distance_from_route

        charger[
            "route_position_percent"
        ] = route_position

        charger[
            "route_relevant"
        ] = True

        route_chargers.append(
            charger
        )

    route_chargers.sort(
        key=lambda charger:
        charger.get(
            "route_position_percent",
            0,
        )
    )

    return route_chargers