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
    between a charger and the verified route.
    """

    if not route_coordinates:
        return None

    # A long route is looked up through a grid. Only an answer that is certainly the nearest one is used (the nearest segment
    # found is closer than a grid cell); anything else goes through the full scan below, so the result never changes.
    if len(route_coordinates) > INDEX_MIN_POINTS:
        index = _cached_index(route_coordinates)
        found = index.nearest(latitude, longitude)
        if found is not None and found[0] <= index.cell:
            return round(found[0], 3)

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

    if len(route_coordinates) > INDEX_MIN_POINTS:
        index = _cached_index(route_coordinates)
        found = index.nearest(latitude, longitude)
        if found is not None and found[0] <= index.cell:
            return index.position_percent(found[1], found[2])

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


INDEX_MIN_POINTS = 300      # shorter routes are scanned directly: it is fast and there is nothing to index
_INDEX_CACHE_SIZE = 4
_index_cache = []           # newest last: (the coordinate list itself, its RouteIndex)


def _cached_index(route_coordinates):
    """The index of this very coordinate list (kept by identity, so a new route never gets an old route's index)."""
    for coords, index in _index_cache:
        if coords is route_coordinates and index.size == len(route_coordinates):
            return index
    index = RouteIndex(route_coordinates, 5.0)
    _index_cache.append((route_coordinates, index))
    del _index_cache[:-_INDEX_CACHE_SIZE]
    return index


class RouteIndex:
    """The route projected once, with its segments filed in a grid, for asking "how far is this point from the route?" many times.

    ``calculate_distance_to_route_km`` re-projects and tests every segment of the route for every point it is asked about; on a
    1,600 km route (tens of thousands of segments) with a couple of thousand candidate chargers that is minutes of work for the
    server. Here a point only tests the segments in the grid cells around it. A segment within ``cell_km`` of a point always
    passes through one of those cells, so for points inside the corridor the answer is exactly the one the full scan gives
    (same projection, same formula, same tie-break), and points further away than a cell are reported as not found.
    """

    MAX_CELLS_PER_SEGMENT = 4096

    def __init__(self, route_coordinates, cell_km):
        self.size = len(route_coordinates)
        self.reference_latitude = route_coordinates[0][1]
        self.cell = max(float(cell_km), 0.5)
        projected = [project_point(lat, lon, self.reference_latitude) for lon, lat in route_coordinates]
        self.xs = [p[0] for p in projected]
        self.ys = [p[1] for p in projected]
        count = len(route_coordinates) - 1
        # Same running sum, in the same order, as calculate_route_position_percent: the percentages match to the last digit.
        self.prefix = [0.0] * (count + 1)
        self.lengths = [0.0] * count
        for i in range(count):
            lon1, lat1 = route_coordinates[i]
            lon2, lat2 = route_coordinates[i + 1]
            self.lengths[i] = haversine_distance_km(lat1, lon1, lat2, lon2)
            self.prefix[i + 1] = self.prefix[i] + self.lengths[i]
        self.total = sum(self.lengths)
        self.grid = {}
        self.long_segments = []
        cell = self.cell
        for i in range(count):
            x0, x1 = sorted((self.xs[i], self.xs[i + 1]))
            y0, y1 = sorted((self.ys[i], self.ys[i + 1]))
            cx0, cx1, cy0, cy1 = math.floor(x0 / cell), math.floor(x1 / cell), math.floor(y0 / cell), math.floor(y1 / cell)
            if (cx1 - cx0 + 1) * (cy1 - cy0 + 1) > self.MAX_CELLS_PER_SEGMENT:
                self.long_segments.append(i)  # a huge straight segment is tested for every point instead of filed in thousands of cells
                continue
            for cx in range(cx0, cx1 + 1):
                for cy in range(cy0, cy1 + 1):
                    self.grid.setdefault((cx, cy), []).append(i)

    def nearest(self, latitude, longitude):
        """(distance_km, segment index, position t along it) of the closest segment, or None when none is within a cell."""
        px, py = project_point(latitude, longitude, self.reference_latitude)
        cx, cy = math.floor(px / self.cell), math.floor(py / self.cell)
        candidates = set(self.long_segments)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                candidates.update(self.grid.get((cx + dx, cy + dy), ()))
        if not candidates:
            return None
        best = None
        xs, ys = self.xs, self.ys
        for i in sorted(candidates):  # index order, so a tie goes to the earlier segment as in the full scan
            ax, ay, bx, by = xs[i], ys[i], xs[i + 1], ys[i + 1]
            dx, dy = bx - ax, by - ay
            if dx == 0 and dy == 0:
                t = 0
            else:
                t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
            distance = math.sqrt((px - (ax + t * dx)) ** 2 + (py - (ay + t * dy)) ** 2)
            if best is None or distance < best[0]:
                best = (distance, i, t)
        return best

    def position_percent(self, segment, t):
        if self.total <= 0:
            return 0.0
        return round((self.prefix[segment] + self.lengths[segment] * t) / self.total * 100, 2)


def filter_chargers_near_route(
    route_geometry,
    stations,
    corridor_km=5.0,
):
    """
    Return only charging stations within the
    configured corridor of the actual verified route.

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

    # One index of the route for all the stations (see RouteIndex). The corridor is the grid cell, so a station inside it is found exactly.
    index = RouteIndex(route_coordinates, corridor_km)

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

        found = index.nearest(latitude, longitude)

        if found is None:
            continue

        distance_from_route = round(found[0], 3)

        if distance_from_route > corridor_km:
            continue

        route_position = index.position_percent(found[1], found[2])

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