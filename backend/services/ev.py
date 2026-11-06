from dataclasses import dataclass


@dataclass
class EVProfile:
    max_range_km: float
    battery_percent: float
    reserve_percent: float = 15.0


def calculate_usable_range(
    ev: EVProfile,
) -> float:
    """
    Calculate how far the EV can travel
    before reaching the required battery reserve.
    """

    usable_battery_percent = (
        ev.battery_percent - ev.reserve_percent
    )

    if usable_battery_percent <= 0:
        return 0.0

    return (
        ev.max_range_km
        * usable_battery_percent
        / 100
    )


def calculate_remaining_battery(
    ev: EVProfile,
    distance_km: float,
) -> float:
    """
    Estimate remaining battery percentage
    after travelling a given distance.
    """

    if ev.max_range_km <= 0:
        raise ValueError(
            "Maximum range must be greater than zero."
        )

    battery_used = (
        distance_km
        / ev.max_range_km
        * 100
    )

    remaining = (
        ev.battery_percent
        - battery_used
    )

    return round(
        max(remaining, 0),
        2,
    )


def can_reach_destination(
    ev: EVProfile,
    distance_km: float,
) -> bool:
    """
    Check whether the EV can travel the distance
    while maintaining the required reserve.
    """

    usable_range = calculate_usable_range(ev)

    return distance_km <= usable_range