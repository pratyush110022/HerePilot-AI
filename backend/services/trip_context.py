from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from threading import Lock
from typing import Any, Optional
from uuid import UUID, uuid4

VEHICLE_TYPES = ("petrol", "diesel", "hybrid", "electric")
MAX_SESSIONS = 500


@dataclass
class PlaceContext:
    name: str
    display_name: str
    latitude: float
    longitude: float
    role: str = "stop"
    result_type: Optional[str] = None  # HERE resultType, e.g. "locality"; None for older sessions


@dataclass
class VehicleContext:
    make: Optional[str] = None
    model: Optional[str] = None
    max_range_km: Optional[float] = None
    battery_capacity_kwh: Optional[float] = None
    current_battery_percent: Optional[float] = None
    source: str = "user_declared"

    def has_core_range_context(self) -> bool:
        return (
            self.max_range_km is not None
            and self.current_battery_percent is not None
        )


@dataclass
class TripContext:
    session_id: str
    stops: list[PlaceContext] = field(default_factory=list)
    vehicle: Optional[VehicleContext] = None
    reserve_percent: float = 15.0
    target_charge_percent: float = 80.0
    corridor_km: float = 5.0
    route: Optional[dict[str, Any]] = None
    chargers: list[dict[str, Any]] = field(default_factory=list)
    pois: list[dict[str, Any]] = field(default_factory=list)
    ev_plan: Optional[dict[str, Any]] = None
    weather: dict[str, Any] = field(default_factory=dict)
    last_action: Optional[dict[str, Any]] = None
    turn_count: int = 0
    # --- general-assistant state -------------------------------------------------
    vehicle_type: Optional[str] = None      # petrol | diesel | hybrid | electric; informational unless a feature uses it
    travel_mode: str = "car"                # HERE transport mode used for routing
    ev_mode: bool = False                   # the optional EV Planner is switched on for this trip
    ev_status: str = "not_requested"        # not_requested | needs_vehicle | calculated | failed
    focus: Optional[dict[str, Any]] = None  # the one place the user is currently talking about
    pois_label: Optional[str] = None        # human description of what ``pois`` currently holds
    route_revision: int = 0                 # bumps whenever the route changes; dependent results are cleared
    user_location: Optional[dict[str, Any]] = None  # {latitude, longitude, accuracy_m} the user chose to share; kept in memory only
    # The last few turns (what was asked, what was answered, which places were shown) so a follow-up can be understood.
    # Server-side only: it is never sent to the browser.
    memory: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """The session as the browser sees it. The private conversation memory is left out."""
        data = asdict(self)
        data.pop("memory", None)
        return data

    def trip_names(self) -> list[str]:
        return [stop.name for stop in self.stops]

    @property
    def has_route(self) -> bool:
        return bool(self.route and self.route.get("geometry"))

    def clear_derived(self) -> None:
        self.route = None
        self.invalidate_route_dependents()

    def invalidate_route_dependents(self) -> None:
        """Drop everything computed from a route that no longer exists or has changed."""
        self.chargers = []
        self.pois = []
        self.pois_label = None
        self.ev_plan = None
        self.ev_status = "needs_vehicle" if self.ev_mode else "not_requested"
        self.weather = {}
        self.focus = None
        self.route_revision += 1


def is_valid_session_id(value: object) -> bool:
    try:
        return isinstance(value, str) and str(UUID(value)) == value.lower()
    except (ValueError, AttributeError, TypeError):
        return False


class TripContextStore:
    """Thread-safe in-memory conversation store for local/single-instance use.

    Bounded: the least recently used session is evicted past ``MAX_SESSIONS`` so a client
    cannot grow memory without limit.
    """

    def __init__(self, max_sessions: int = MAX_SESSIONS) -> None:
        self._contexts: "OrderedDict[str, TripContext]" = OrderedDict()
        self._lock = Lock()
        self._max = max_sessions

    def _evict_locked(self) -> None:
        while len(self._contexts) > self._max:
            self._contexts.popitem(last=False)

    def create(self) -> TripContext:
        session_id = str(uuid4())
        context = TripContext(session_id=session_id)
        with self._lock:
            self._contexts[session_id] = context
            self._evict_locked()
        return deepcopy(context)

    def find(self, session_id: str) -> Optional[TripContext]:
        """Return a copy of an existing session, or ``None``. Never creates one."""
        with self._lock:
            context = self._contexts.get(session_id)
            if context is None:
                return None
            self._contexts.move_to_end(session_id)
            return deepcopy(context)

    def get(self, session_id: str) -> TripContext:
        """Existing session, or a fresh one registered under ``session_id`` (legacy behaviour)."""
        with self._lock:
            context = self._contexts.get(session_id)
            if context is None:
                context = TripContext(session_id=session_id)
                self._contexts[session_id] = context
                self._evict_locked()
            else:
                self._contexts.move_to_end(session_id)
            return deepcopy(context)

    def save(self, context: TripContext) -> TripContext:
        with self._lock:
            self._contexts[context.session_id] = deepcopy(context)
            self._contexts.move_to_end(context.session_id)
            self._evict_locked()
        return deepcopy(context)

    def delete(self, session_id: str) -> bool:
        with self._lock:
            return self._contexts.pop(session_id, None) is not None

    def size(self) -> int:
        with self._lock:
            return len(self._contexts)
