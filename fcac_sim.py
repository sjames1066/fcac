"""Watch MSFS (through SimConnect) for the moment you have landed and slowed down, and read the local time there.

Everything that decides "landed and decelerated" is in LandingDetector, which knows nothing about the sim, so it can
be tested on its own. SimWatcher feeds it from SimConnect.
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("fcac")


class LandingDetector:
    """Feed it once a second. Returns "takeoff" the first time you are properly airborne, "landed" once when, after
    touching down, the ground speed falls to `slow_kt`, otherwise None. A touch-and-go (airborne again before slowing
    down) triggers nothing."""

    def __init__(self, slow_kt: float = 60.0, airborne_ft: float = 300.0):
        self.slow_kt, self.airborne_ft = slow_kt, airborne_ft
        self.flown = False      # has been properly airborne since the last landing
        self.touched = False    # on the ground again after that flight

    def feed(self, on_ground: bool, agl_ft: float, ground_kt: float) -> str | None:
        if not on_ground:
            self.touched = False
            if agl_ft >= self.airborne_ft and not self.flown:
                self.flown = True
                return "takeoff"
            return None
        if self.flown:
            self.touched = True
        if self.flown and self.touched and ground_kt <= self.slow_kt:
            self.flown = self.touched = False
            return "landed"
        return None


def round_clock(seconds: float, step_min: int = 5) -> str:
    """Seconds since local midnight -> "HH:MM" to the nearest `step_min` minutes."""
    minutes = int(round(seconds / 60 / step_min)) * step_min % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


class SimWatcher:
    """Background thread: connects to MSFS when it is running and calls
    on_event("takeoff") or on_event("landed", "HH:MM") (local time where you landed)."""

    def __init__(self, on_event, running, stop: threading.Event, slow_kt: float = 60.0):
        self.on_event, self.running, self.stop = on_event, running, stop
        self.detector = LandingDetector(slow_kt)
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def loop(self) -> None:
        try:
            from SimConnect import AircraftRequests, SimConnect
        except Exception as exc:
            log.info("Live landing time unavailable (SimConnect library missing): %s", exc)
            return
        sm = requests = None
        while not self.stop.is_set():
            try:
                if sm is None:
                    if not self.running():
                        self.stop.wait(15)
                        continue
                    sm = SimConnect()
                    requests = AircraftRequests(sm, _time=500)
                    log.info("Connected to MSFS (SimConnect)")
                    self.detector = LandingDetector(self.detector.slow_kt)
                ground = requests.get("SIM_ON_GROUND")
                agl = requests.get("PLANE_ALT_ABOVE_GROUND")
                speed = requests.get("GROUND_VELOCITY")
                if ground is None or agl is None or speed is None:
                    self.stop.wait(1)
                    continue
                event = self.detector.feed(bool(ground), float(agl), float(speed))
                if event == "takeoff":
                    self.on_event("takeoff", None)
                elif event == "landed":
                    local = requests.get("LOCAL_TIME")
                    if local is None:
                        log.warning("Landed, but MSFS did not give the local time")
                        self.on_event("landed", None)
                    else:
                        clock = round_clock(float(local))
                        log.info("Landed and slowed down - local time here is %s (sim says %.0f s since midnight)", clock, float(local))
                        self.on_event("landed", clock)
                self.stop.wait(1)
            except Exception as exc:  # sim closed, connection lost...: try again later
                log.info("SimConnect: %s - will retry", exc)
                sm = requests = None
                self.stop.wait(10)
