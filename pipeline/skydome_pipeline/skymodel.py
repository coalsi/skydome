"""What should be in the sky right now besides the stars (DECISIONS.md D-030):
the Sun, the Moon and the planets, satellites, and aircraft. The sky
watcher (skywatch.py) checks everything it sees against this; the web page
labels them.

* **Planets:** JPL's approximate Keplerian elements (Standish, valid
  1800-2050, a few arcminutes), precessed to the date like the stars.
* **Moon:** the Astronomical Almanac's low-precision series (~0.3°), with
  its parallax for the site (up to 1°).
* **Satellites:** two-line elements from CelesTrak (every active one,
  refreshed daily) through SGP4 (the `sgp4` package), all at once; a
  satellite is only seen while it's in sunlight and the site isn't.
* **Aircraft:** ADS-B as a dump1090/readsb/tar1090 receiver serves it
  (aircraft.json), each moved on along its track for the seconds since its
  last position.

Positions are azimuth (0 = north, clockwise) and altitude in degrees, with
refraction, like apparent_altaz for the stars.
"""

from __future__ import annotations

import json
import math
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from .stars import _altaz_of_date, _precession, julian_date, refraction_deg

EARTH_RADIUS_KM = 6378.137
WGS84_F = 1 / 298.257223563
FT = 0.3048
CELESTRAK_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP=active&FORMAT=tle"

# ---- the Sun, the Moon and the planets ----------------------------------------------------

# a (au), e, I, L, longitude of perihelion, longitude of the node (degrees), and their
# rates per Julian century from J2000 (E. M. Standish, JPL, Table 1: 1800-2050)
_ELEMENTS = {
    "Mercury": ((0.38709927, 0.20563593, 7.00497902, 252.25032350, 77.45779628, 48.33076593),
                (0.00000037, 0.00001906, -0.00594749, 149472.67411175, 0.16047689, -0.12534081)),
    "Venus": ((0.72333566, 0.00677672, 3.39467605, 181.97909950, 131.60246718, 76.67984255),
              (0.00000390, -0.00004107, -0.00078890, 58517.81538729, 0.00268329, -0.27769418)),
    "Earth": ((1.00000261, 0.01671123, -0.00001531, 100.46457166, 102.93768193, 0.0),
              (0.00000562, -0.00004392, -0.01294668, 35999.37244981, 0.32327364, 0.0)),
    "Mars": ((1.52371034, 0.09339410, 1.84969142, -4.55343205, -23.94362959, 49.55953891),
             (0.00001847, 0.00007882, -0.00813131, 19140.30268499, 0.44441088, -0.29257343)),
    "Jupiter": ((5.20288700, 0.04838624, 1.30439695, 34.39644051, 14.72847983, 100.47390909),
                (-0.00011607, -0.00013253, -0.00183714, 3034.74612775, 0.21252668, 0.20469106)),
    "Saturn": ((9.53667594, 0.05386179, 2.48599187, 49.95424423, 92.59887831, 113.66242448),
               (-0.00125060, -0.00050991, 0.00193609, 1222.49362201, -0.41897216, -0.28867794)),
    "Uranus": ((19.18916464, 0.04725744, 0.77263783, 313.23810451, 170.95427630, 74.01692503),
               (-0.00196176, -0.00004397, -0.00242939, 428.48202785, 0.40805281, 0.04240589)),
    "Neptune": ((30.06992276, 0.00859048, 1.77004347, -55.12002969, 44.96476227, 131.78422574),
                (0.00026291, 0.00005105, 0.00035372, 218.45945325, -0.32241464, -0.00508664)),
}
PLANETS = ("Mercury", "Venus", "Mars", "Jupiter", "Saturn", "Uranus", "Neptune")
_OBLIQUITY_J2000 = math.radians(23.43928)


def _heliocentric(name: str, T: float) -> np.ndarray:
    """Heliocentric ecliptic J2000 position (au) T Julian centuries after J2000."""
    base, rate = _ELEMENTS[name]
    a, e, inc, L, peri, node = (b + r * T for b, r in zip(base, rate))
    M = math.radians((L - peri + 180) % 360 - 180)
    E = M + e * math.sin(M)
    for _ in range(8):   # Kepler's equation by Newton's method
        E -= (E - e * math.sin(E) - M) / (1 - e * math.cos(E))
    xp, yp = a * (math.cos(E) - e), a * math.sqrt(1 - e * e) * math.sin(E)
    w, O, i = math.radians(peri - node), math.radians(node), math.radians(inc)
    cw, sw, cO, sO, ci, si = math.cos(w), math.sin(w), math.cos(O), math.sin(O), math.cos(i), math.sin(i)
    return np.array([(cw * cO - sw * sO * ci) * xp + (-sw * cO - cw * sO * ci) * yp,
                     (cw * sO + sw * cO * ci) * xp + (-sw * sO + cw * cO * ci) * yp,
                     (sw * si) * xp + (cw * si) * yp])


def _ecliptic_to_equatorial(v: np.ndarray, eps: float) -> np.ndarray:
    c, s = math.cos(eps), math.sin(eps)
    return np.array([v[0], c * v[1] - s * v[2], s * v[1] + c * v[2]])


def _apparent(ra: float, dec: float, lat: float, lon: float, jd: float) -> tuple[float, float]:
    az, alt = _altaz_of_date(np.array([ra]), np.array([dec]), lat, lon, jd)
    return float(az[0]), float(alt[0] + refraction_deg(alt)[0])


def planet_altaz(name: str, lat: float, lon: float, t: datetime) -> tuple[float, float, float]:
    """A planet's azimuth and altitude (degrees, refracted) and distance (au)."""
    jd = julian_date(t)
    T = (jd - 2451545.0) / 36525.0
    g = _heliocentric(name, T) - _heliocentric("Earth", T)
    d = _precession(jd) @ _ecliptic_to_equatorial(g, _OBLIQUITY_J2000)
    r = float(np.linalg.norm(d))
    az, alt = _apparent(math.atan2(d[1], d[0]), math.asin(d[2] / r), lat, lon, jd)
    return az, alt, r


def moon_altaz(lat: float, lon: float, t: datetime) -> tuple[float, float, float]:
    """The Moon's azimuth and altitude (degrees, refracted, with parallax) and
    its phase (0 new .. 1 full)."""
    jd = julian_date(t)
    T = (jd - 2451545.0) / 36525.0

    def s(a, b):
        return math.sin(math.radians(a + b * T))

    def c(a, b):
        return math.cos(math.radians(a + b * T))

    lam = (218.32 + 481267.881 * T + 6.29 * s(135.0, 477198.87) - 1.27 * s(259.3, -413335.36)
           + 0.66 * s(235.7, 890534.22) + 0.21 * s(269.9, 954397.74) - 0.19 * s(357.5, 35999.05)
           - 0.11 * s(186.5, 966404.03))
    beta = 5.13 * s(93.3, 483202.02) + 0.28 * s(228.2, 960400.89) - 0.28 * s(318.3, 6003.15) - 0.17 * s(217.6, -407332.21)
    par = (0.9508 + 0.0518 * c(135.0, 477198.87) + 0.0095 * c(259.3, -413335.36) + 0.0078 * c(235.7, 890534.22)
           + 0.0028 * c(269.9, 954397.74))
    lam_r, beta_r = math.radians(lam), math.radians(beta)
    v = np.array([math.cos(beta_r) * math.cos(lam_r), math.cos(beta_r) * math.sin(lam_r), math.sin(beta_r)])
    eps = math.radians(23.439 - 0.0000004 * (jd - 2451545.0))
    d = _ecliptic_to_equatorial(v, eps)
    az, alt_geo = _altaz_of_date(np.array([math.atan2(d[1], d[0])]), np.array([math.asin(d[2])]), lat, lon, jd)
    alt = float(alt_geo[0]) - par * math.cos(math.radians(float(alt_geo[0])))   # seen from the ground, not the centre
    # Phase from the Moon's elongation from the Sun (same low-precision Sun as sun_altaz)
    n = jd - 2451545.0
    sun_lam = (280.460 + 0.9856474 * n) + 1.915 * math.sin(math.radians(357.528 + 0.9856003 * n))
    elong = math.acos(math.cos(beta_r) * math.cos(lam_r - math.radians(sun_lam)))
    return float(az[0]), alt + float(refraction_deg(np.array([alt]))[0]), (1 - math.cos(elong)) / 2


# ---- satellites ------------------------------------------------------------------------

def _site_ecef(lat: float, lon: float, h_m: float) -> np.ndarray:
    phi, lam = math.radians(lat), math.radians(lon)
    e2 = WGS84_F * (2 - WGS84_F)
    n = EARTH_RADIUS_KM / math.sqrt(1 - e2 * math.sin(phi) ** 2)
    h = h_m / 1000
    return np.array([(n + h) * math.cos(phi) * math.cos(lam), (n + h) * math.cos(phi) * math.sin(lam),
                     (n * (1 - e2) + h) * math.sin(phi)])


def _enu(lat: float, lon: float) -> np.ndarray:
    """Rows: east, north, up in ECEF."""
    phi, lam = math.radians(lat), math.radians(lon)
    return np.array([[-math.sin(lam), math.cos(lam), 0.0],
                     [-math.sin(phi) * math.cos(lam), -math.sin(phi) * math.sin(lam), math.cos(phi)],
                     [math.cos(phi) * math.cos(lam), math.cos(phi) * math.sin(lam), math.sin(phi)]])


def _look(ecef: np.ndarray, lat: float, lon: float, h_m: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """ECEF points (..., 3) km -> azimuth, altitude (degrees, refracted) and range (km) from the site."""
    rel = (ecef - _site_ecef(lat, lon, h_m)) @ _enu(lat, lon).T
    rng = np.linalg.norm(rel, axis=-1)
    alt = np.degrees(np.arcsin(np.clip(rel[..., 2] / np.maximum(rng, 1e-9), -1, 1)))
    az = np.degrees(np.arctan2(rel[..., 0], rel[..., 1])) % 360
    return az, alt + refraction_deg(alt), rng


def _gmst(jd: float) -> float:
    T = (jd - 2451545.0) / 36525.0
    return math.radians((280.46061837 + 360.98564736629 * (jd - 2451545.0) + 0.000387933 * T * T) % 360)


def _sun_vector(jd: float) -> np.ndarray:
    """Unit vector to the Sun, equator of date (low precision, as sun_altaz)."""
    n = jd - 2451545.0
    g = math.radians(357.528 + 0.9856003 * n)
    lam = math.radians(280.460 + 0.9856474 * n + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))
    eps = math.radians(23.439 - 0.0000004 * n)
    return np.array([math.cos(lam), math.cos(eps) * math.sin(lam), math.sin(eps) * math.sin(lam)])


@dataclass
class Satellites:
    """Every satellite in a TLE file, propagated together."""
    names: list[str]
    norad: np.ndarray
    _array: object = field(repr=False)

    @classmethod
    def parse(cls, text: str) -> "Satellites":
        from sgp4.api import Satrec, SatrecArray

        lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
        names, sats, ids = [], [], []
        i = 0
        while i < len(lines):
            if lines[i].startswith("1 ") and i + 1 < len(lines) and lines[i + 1].startswith("2 "):
                name, l1, l2 = f"NORAD {lines[i][2:7].strip()}", lines[i], lines[i + 1]   # no name line
                i += 2
            elif i + 2 < len(lines) and lines[i + 1].startswith("1 ") and lines[i + 2].startswith("2 "):
                name, l1, l2 = lines[i], lines[i + 1], lines[i + 2]
                i += 3
            else:
                i += 1
                continue
            try:
                sats.append(Satrec.twoline2rv(l1, l2))
            except ValueError:
                continue
            names.append(name.strip())
            ids.append(int(l1[2:7]))
        if not sats:
            raise ValueError("no satellites in the TLE text")
        return cls(names, np.array(ids), SatrecArray(sats))

    @classmethod
    def load(cls, path: Path, max_age_h: float = 24, url: str = CELESTRAK_URL) -> "Satellites":
        """From path, downloading CelesTrak's file first if it's missing or older than max_age_h
        (an old file is still used if the download fails)."""
        path = Path(path)
        if not path.exists() or time.time() - path.stat().st_mtime > max_age_h * 3600:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "SkyDome all-sky camera"})
                text = urllib.request.urlopen(req, timeout=60).read().decode()
                cls.parse(text)   # only replace a good file with a good one
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(text)
                tmp.replace(path)
            except (OSError, ValueError):
                if not path.exists():
                    raise
        return cls.parse(path.read_text())

    def look(self, lat: float, lon: float, h_m: float, t: datetime) -> dict[str, np.ndarray]:
        """Every satellite's azimuth, altitude, range (km), whether it's in sunlight, and SGP4's error code."""
        jd = julian_date(t)
        jd_day, jd_frac = math.floor(jd), jd - math.floor(jd)
        err, r, _ = self._array.sgp4(np.array([float(jd_day)]), np.array([jd_frac]))
        r = r[:, 0, :]
        th = _gmst(jd)   # TEME -> Earth-fixed: turn by sidereal time (polar motion ignored: metres)
        ecef = np.stack([np.cos(th) * r[:, 0] + np.sin(th) * r[:, 1], -np.sin(th) * r[:, 0] + np.cos(th) * r[:, 1],
                         r[:, 2]], axis=1)
        az, alt, rng = _look(ecef, lat, lon, h_m)
        s = _sun_vector(jd)
        along = r @ s
        sunlit = (along > 0) | (np.linalg.norm(r - along[:, None] * s, axis=1) > EARTH_RADIUS_KM)
        ok = (err[:, 0] == 0) & np.isfinite(alt)
        return {"az": az, "alt": alt, "range_km": rng, "sunlit": sunlit, "ok": ok}


# ---- aircraft ----------------------------------------------------------------------------

def aircraft_from_json(doc: dict, now: float | None = None) -> list[dict]:
    """dump1090/readsb aircraft.json -> aircraft with a position: hex, flight,
    lat, lon, alt_ft (barometric, else geometric), speed_kt, track, age_s.
    On the ground and position-less ones are left out."""
    now = now if now is not None else float(doc.get("now") or time.time())
    out = []
    for a in doc.get("aircraft") or []:
        if a.get("lat") is None or a.get("lon") is None:
            continue
        alt = a.get("alt_baro", a.get("altitude", a.get("alt_geom")))
        if alt == "ground" or alt is None:
            continue
        seen = float(a.get("seen_pos", a.get("seen", 0)) or 0)
        if seen > 60:
            continue
        out.append({"hex": a.get("hex", ""), "flight": (a.get("flight") or "").strip(),
                    "lat": float(a["lat"]), "lon": float(a["lon"]), "alt_ft": float(alt),
                    "speed_kt": float(a.get("gs", a.get("speed", 0)) or 0), "track": float(a.get("track", 0) or 0),
                    "age_s": seen, "category": a.get("category") or ""})
    return out


def aircraft_look(planes: list[dict], lat: float, lon: float, h_m: float, shift_s: float = 0.0) -> list[dict]:
    """Where each aircraft is in the sky: moved on along its track for the
    seconds since its last position (plus shift_s: the time wanted minus
    when the feed was read), then azimuth, altitude, range (km)."""
    out = []
    for p in planes:
        d_km = p["speed_kt"] * 1.852 / 3600 * (p["age_s"] + shift_s)
        tr = math.radians(p["track"])
        plat = p["lat"] + math.degrees(d_km * math.cos(tr) / 6371.0)
        plon = p["lon"] + math.degrees(d_km * math.sin(tr) / (6371.0 * math.cos(math.radians(p["lat"]))))
        e = _site_ecef(plat, plon, p["alt_ft"] * FT)
        az, alt, rng = _look(e[None, :], lat, lon, h_m)
        out.append({**p, "az": float(az[0]), "alt": float(alt[0]), "range_km": float(rng[0])})
    return out


class AdsbFeed:
    """A receiver's aircraft.json, fetched at most once a second."""

    def __init__(self, url: str, timeout: float = 2.0):
        self.url = url
        self.timeout = timeout
        self._at = 0.0
        self._planes: list[dict] = []
        self.ok = False
        self.fetched_at = 0.0   # Unix time of the planes' age_s

    def planes(self) -> list[dict]:
        if time.time() - self._at >= 1.0:
            self._at = time.time()
            try:
                with urllib.request.urlopen(self.url, timeout=self.timeout) as r:
                    self._planes = aircraft_from_json(json.load(r))
                self.fetched_at = time.time()
                self.ok = True
            except (OSError, ValueError):
                self.ok = False   # keep the last ones; they age out by their age_s
        return self._planes


# ---- all of it together -------------------------------------------------------------------

@dataclass
class SkyModel:
    """Everything that should be up there, for one site. Satellites from
    tle_path (refresh() fetches it daily), aircraft from adsb_url."""
    lat: float
    lon: float
    elev_m: float = 0.0
    tle_path: Path | None = None
    adsb_url: str | None = None
    _sats: Satellites | None = field(default=None, repr=False)
    _sats_mtime: float = field(default=0.0, repr=False)
    _adsb: AdsbFeed | None = field(default=None, repr=False)

    def refresh(self) -> None:
        """Fetch the satellites if the file is missing or a day old (slow: call from a background thread)."""
        if self.tle_path:
            Satellites.load(self.tle_path)

    def satellites(self) -> Satellites | None:
        if not self.tle_path or not Path(self.tle_path).exists():
            return None
        m = Path(self.tle_path).stat().st_mtime
        if self._sats is None or m != self._sats_mtime:
            try:
                self._sats = Satellites.parse(Path(self.tle_path).read_text())
                self._sats_mtime = m
            except (OSError, ValueError):
                pass
        return self._sats

    def planes(self) -> list[dict] | None:
        if not self.adsb_url:
            return None
        if self._adsb is None or self._adsb.url != self.adsb_url:
            self._adsb = AdsbFeed(self.adsb_url)
        return self._adsb.planes()

    def snapshot(self, t: datetime, min_alt: float = -2.0) -> dict:
        """What's up at t: bodies (Sun, Moon, planets), satellites and aircraft,
        each with az/alt in degrees. Satellites: only those above min_alt."""
        from .stars import sun_altaz

        saz, salt = sun_altaz(self.lat, self.lon, t)
        bodies = [{"name": "Sun", "kind": "sun", "az": saz, "alt": float(salt + refraction_deg(np.array([salt]))[0])}]
        maz, malt, phase = moon_altaz(self.lat, self.lon, t)
        bodies.append({"name": "Moon", "kind": "moon", "az": maz, "alt": malt, "phase": round(phase, 3)})
        for name in PLANETS:
            az, alt, dist = planet_altaz(name, self.lat, self.lon, t)
            bodies.append({"name": name, "kind": "planet", "az": az, "alt": alt, "au": round(dist, 4)})
        sats = []
        s = self.satellites()
        if s is not None:
            look = s.look(self.lat, self.lon, self.elev_m, t)
            for i in np.nonzero(look["ok"] & (look["alt"] > min_alt))[0]:
                sats.append({"name": s.names[i], "norad": int(s.norad[i]), "az": float(look["az"][i]),
                             "alt": float(look["alt"][i]), "range_km": float(look["range_km"][i]),
                             "sunlit": bool(look["sunlit"][i])})
        planes = self.planes()
        planes = None if planes is None else [p for p in aircraft_look(planes, self.lat, self.lon, self.elev_m)
                                              if p["alt"] > min_alt]
        return {"sun_alt": float(salt), "bodies": bodies, "satellites": sats, "planes": planes,
                "adsb": None if self._adsb is None else self._adsb.ok}
