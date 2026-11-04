"""The sky watcher (DECISIONS.md D-030): everything that moves through the
camera's view, followed, identified and logged, and anything that can't be
identified flagged.

Each frame (skydome-live's half-size 16-bit copy) is compared with the sky
of the last few frames; what's new and bigger than noise becomes a
detection, at an azimuth and altitude through the camera's calibration.
Known fixed lights (bright stars, the planets, the Moon, the Sun) and the
ground mask are left out. Detections are joined into tracks; a long night
frame shows a moving thing as a streak, which carries its own direction and
speed.

When a track ends it's identified, most certain first:

1. **Aircraft:** its path follows an ADS-B aircraft (flight, altitude).
2. **Satellite:** it follows a sunlit satellite from the orbits (TLEs).
3. **What you've taught it:** the nearest of the objects you've labelled,
   by how they moved and looked (speed, straightness, size, brightness,
   blinking, how long, how high, day or night). Every label you give on the
   page is another example.
4. **Rules** until there are enough labels: a lone straight streak at night
   is a meteor (and which shower, from its radiant); big and slow is cloud.

What's left is **unknown**: kept with its pictures, listed first on the page,
and an alert if one's set up.
"""

from __future__ import annotations

import io
import json
import math
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .camera import Camera
from .geometry import altaz_to_vec, vec_to_altaz
from .skymodel import SkyModel, aircraft_look

LABELS = ("plane", "helicopter", "satellite", "meteor", "bird", "bat", "insect", "cloud", "balloon", "drone",
          "star", "planet", "moon", "lens", "noise", "unknown")

# Major meteor showers: name, active from / to (month, day), radiant RA, Dec at the peak (degrees)
SHOWERS = (
    ("Quadrantids", (12, 28), (1, 12), 230.0, 49.0),
    ("Lyrids", (4, 14), (4, 30), 271.0, 34.0),
    ("Eta Aquariids", (4, 19), (5, 28), 338.0, -1.0),
    ("Southern Delta Aquariids", (7, 12), (8, 23), 340.0, -16.0),
    ("Perseids", (7, 17), (8, 24), 48.0, 58.0),
    ("Draconids", (10, 6), (10, 10), 262.0, 54.0),
    ("Orionids", (10, 2), (11, 7), 95.0, 16.0),
    ("Southern Taurids", (9, 10), (11, 20), 52.0, 15.0),
    ("Northern Taurids", (10, 20), (12, 10), 58.0, 22.0),
    ("Leonids", (11, 6), (11, 30), 152.0, 22.0),
    ("Geminids", (12, 4), (12, 20), 112.0, 33.0),
    ("Ursids", (12, 17), (12, 26), 217.0, 76.0),
)


# ---- frames from skydome-live -------------------------------------------------------------

@dataclass
class Frame:
    seq: int
    t: float              # middle of the exposure, Unix seconds
    exposure_s: float
    gain: int
    image: np.ndarray     # luma, float32, 0..255 scale


def read_tap(directory: Path, name: str, last_seq: int | None = None) -> Frame | None:
    """The newest frame skydome-live left for the watcher, or None if there's no new one."""
    meta_path = Path(directory) / f"{name}.json"
    try:
        meta = json.loads(meta_path.read_text())
        if meta["seq"] == last_seq:
            return None
        raw = np.fromfile(Path(directory) / f"{name}.y16", dtype=np.uint16)
    except (OSError, ValueError, KeyError):
        return None
    w, h = int(meta["width"]), int(meta["height"])
    if raw.size != w * h:
        return None   # caught between the picture and its description: next time
    return Frame(int(meta["seq"]), float(meta["time"]), meta["exposure_us"] / 1e6, int(meta.get("gain", 0)),
                 raw.reshape(h, w).astype(np.float32) / float(meta.get("scale", 256)))


# ---- detections and tracks ----------------------------------------------------------------

@dataclass
class Detection:
    t: float
    exposure_s: float
    u: float               # centroid, frame pixels
    v: float
    az: float
    alt: float
    area: int              # pixels
    flux: float            # above the background
    peak: float
    length_deg: float      # along the major axis
    width_deg: float
    streak: bool           # elongated in a long frame: moved during the exposure
    ends: tuple | None = None   # a streak's two ends, (az, alt) each
    crop: np.ndarray | None = field(default=None, repr=False)


@dataclass
class Track:
    id: int
    dets: list[Detection] = field(default_factory=list)
    adsb: dict = field(default_factory=dict)    # hex -> [votes, callsign, detail]
    sats: dict = field(default_factory=dict)    # norad -> [votes, name, detail]

    @property
    def last(self) -> Detection:
        return self.dets[-1]

    def velocity(self) -> np.ndarray:
        """Angular velocity, unit vectors per second (zero until it has moved twice)."""
        if len(self.dets) < 2:   # a streak shows its line, not which way along it: no guess
            return np.zeros(3)
        a, b = self.dets[-2], self.dets[-1]
        dt = max(b.t - a.t, 1e-3)
        return (altaz_to_vec(b.az, b.alt) - altaz_to_vec(a.az, a.alt)) / dt

    def predict(self, t: float) -> np.ndarray:
        p = altaz_to_vec(self.last.az, self.last.alt) + self.velocity() * (t - self.last.t)
        return p / np.linalg.norm(p)


def angle_deg(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.degrees(np.arccos(np.clip(np.sum(a * b, axis=-1), -1, 1)))


def track_features(tr: Track, sun_alt: float) -> dict:
    """How it moved and looked, for the classifier and the log."""
    d = tr.dets
    pts = altaz_to_vec(np.array([x.az for x in d]), np.array([x.alt for x in d]))
    dur = d[-1].t - d[0].t + d[-1].exposure_s
    path = float(np.sum(angle_deg(pts[1:], pts[:-1]))) if len(d) > 1 else 0.0
    net = float(angle_deg(pts[0], pts[-1])) if len(d) > 1 else 0.0
    streak_len = max((x.length_deg for x in d if x.streak), default=0.0)
    if len(d) > 1:
        speed = path / max(d[-1].t - d[0].t, 1e-3)
    else:
        speed = streak_len / max(d[0].exposure_s, 1e-3)   # at least: a meteor's streak is shorter than the exposure
    flux = np.array([x.flux for x in d])
    return {
        "speed_dps": speed,
        "straightness": net / path if path > 0 else 1.0,
        "duration_s": dur,
        "size_deg": float(np.median([x.width_deg for x in d])),
        "streak_deg": streak_len,
        "brightness": float(np.log10(max(np.median(flux), 1.0))),
        "blink": float(np.std(flux) / max(np.mean(flux), 1e-6)),
        "frames": len(d),
        "alt": float(np.mean([x.alt for x in d])),
        "night": float(sun_alt < -6),
        "streak_frac": float(np.mean([x.streak for x in d])),
    }


FEATURES = ("speed_dps", "straightness", "duration_s", "size_deg", "streak_deg", "brightness", "blink", "frames",
            "alt", "night", "streak_frac")


def _vector(f: dict) -> np.ndarray:
    """Features on comparable scales (logs for the ones that span decades)."""
    return np.array([math.log10(f["speed_dps"] + 1e-3), f["straightness"], math.log10(f["duration_s"] + 0.05),
                     math.log10(f["size_deg"] + 1e-3), math.log10(f["streak_deg"] + 0.1), f["brightness"],
                     f["blink"], math.log10(f["frames"]), f["alt"] / 30, f["night"] * 2, f["streak_frac"]])


class Classifier:
    """The nearest of the objects you've labelled (k = 5, closer ones count
    more); rules while there are too few labels to go on."""

    def __init__(self, examples: list[tuple[dict, str]]):
        self.labels = [lab for _, lab in examples]
        self.X = np.array([_vector(f) for f, _ in examples]) if examples else np.zeros((0, len(FEATURES)))
        if len(self.X):
            self.mu, self.sd = self.X.mean(0), self.X.std(0) + 0.25
        self.enough = len(examples) >= 5 and len(set(self.labels)) >= 2

    def classify(self, f: dict) -> tuple[str, float, str]:
        """(label, confidence 0..1, "learned" or "rules")."""
        if self.enough:
            z = (_vector(f) - self.mu) / self.sd
            dist = np.linalg.norm((self.X - self.mu) / self.sd - z, axis=1)
            near = np.argsort(dist)[:5]
            w = 1.0 / (dist[near] + 0.3)
            votes: dict[str, float] = {}
            for i, wi in zip(near, w):
                votes[self.labels[i]] = votes.get(self.labels[i], 0.0) + wi
            best = max(votes, key=votes.get)
            conf = votes[best] / sum(votes.values())
            # Far from everything it's been shown: say so rather than guess
            if dist[near[0]] > 4.0:
                return "unknown", 0.3, "learned"
            return best, float(conf), "learned"
        return rules(f)


def rules(f: dict) -> tuple[str, float, str]:
    if f["night"] and f["frames"] == 1 and f["streak_frac"] == 1 and f["straightness"] >= 0.9:
        return "meteor", 0.6, "rules"
    if f["size_deg"] > 1.0 and f["speed_dps"] < 0.2:
        return "cloud", 0.5, "rules"
    if not f["night"] and f["speed_dps"] > 3 and f["duration_s"] < 4:
        return "bird", 0.4, "rules"
    return "unknown", 0.3, "rules"


def shower_of(ends: tuple, t: float, lat: float, lon: float) -> str | None:
    """The active shower whose radiant the streak points back to (within 10° of its line, behind it)."""
    from .stars import _altaz_of_date, julian_date

    dt = datetime.fromtimestamp(t, timezone.utc)
    md = (dt.month, dt.day)
    a, b = altaz_to_vec(*ends[0]), altaz_to_vec(*ends[1])
    n = np.cross(a, b)
    if np.linalg.norm(n) < 1e-9:
        return None
    n /= np.linalg.norm(n)
    best, best_d = None, 10.0
    for name, start, end, ra, dec in SHOWERS:
        active = start <= md <= end if start <= end else (md >= start or md <= end)
        if not active:
            continue
        az, alt = _altaz_of_date(np.radians([ra]), np.radians([dec]), lat, lon, julian_date(dt))
        r = altaz_to_vec(az[0], alt[0])
        off_line = abs(90 - float(np.degrees(np.arccos(np.clip(r @ n, -1, 1)))))
        # The radiant is behind the streak: nearer one end than the other end is, and outside it
        da, db = angle_deg(r, a), angle_deg(r, b)
        behind = min(da, db) > 0 and float(angle_deg(a, b)) < max(da, db)
        if behind and off_line < best_d:
            best, best_d = name, off_line
    return best


# ---- the log ---------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS objects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start REAL NOT NULL, end REAL NOT NULL, frames INTEGER,
    kind TEXT NOT NULL,          -- what it is: the label if you gave one, else the watcher's
    auto_kind TEXT, confidence REAL, source TEXT,   -- the watcher's own: adsb / tle / learned / rules
    label TEXT,                  -- yours, from the page
    match TEXT, match_id TEXT, detail TEXT,         -- flight or satellite name, its id, more as JSON
    shower TEXT, unknown INTEGER NOT NULL DEFAULT 0, alerted INTEGER NOT NULL DEFAULT 0,
    sun_alt REAL, path TEXT, features TEXT, thumb TEXT, clip TEXT
);
CREATE INDEX IF NOT EXISTS objects_start ON objects(start);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def open_log(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=10)
    db.execute("PRAGMA journal_mode=WAL")   # the server reads while the watcher writes
    db.executescript(SCHEMA)
    return db


def labelled_examples(db: sqlite3.Connection) -> list[tuple[dict, str]]:
    rows = db.execute("SELECT features, label FROM objects WHERE label IS NOT NULL AND features IS NOT NULL").fetchall()
    out = []
    for feats, label in rows:
        try:
            out.append((json.loads(feats), label))
        except ValueError:
            continue
    return out


def labels_version(db: sqlite3.Connection) -> str:
    row = db.execute("SELECT value FROM meta WHERE key = 'labels'").fetchone()
    return row[0] if row else ""


# ---- the watcher ----------------------------------------------------------------------------

@dataclass
class WatchConfig:
    sigma: float = 5.0            # brighter than the background by this many noise levels
    min_area: int = 3             # pixels (half size)
    streak_exposure_s: float = 0.25
    streak_elong: float = 3.0
    streak_min_px: float = 8.0
    gate_deg: float = 1.5
    first_step_dps: float = 25.0  # a new track's second sighting can be this fast (a bird close by)
    end_after_s: float = 3.0      # a track not seen for this (or 3 frames) ends
    min_frames_day: int = 3       # a track of points needs this many to count (flicker, noise)
    adsb_gate_deg: float = 2.5
    sat_gate_deg: float = 1.0
    max_blobs: int = 150          # more in one frame: the sky changed (cloud, a jump in exposure); skipped
    star_mag: float = 4.0         # stars brighter than this are masked at night


class Watcher:
    """Frames in, finished tracks out (with their identification)."""

    def __init__(self, cam: Camera, model: SkyModel, star_dirs=None, star_mags=None,
                 horizon: list[float] | None = None, cfg: WatchConfig | None = None,
                 classifier: Classifier | None = None):
        self.cam = cam
        self.model = model
        self.cfg = cfg or WatchConfig()
        self.classifier = classifier or Classifier([])
        self.star_dirs, self.star_mags = star_dirs, star_mags   # ENU now (refreshed by the caller)
        self.bg: np.ndarray | None = None
        self.last: Frame | None = None
        self.tracks: list[Track] = []
        self.next_id = 1
        self.skipped = 0
        self._sky = self._sky_mask(horizon)
        self.px_deg = self._pixel_scale()

    def _pixel_scale(self) -> float:
        c = np.array([self.cam.width / 2, self.cam.height / 2])
        a = self.cam.pixel_to_world(c[0], c[1])
        b = self.cam.pixel_to_world(c[0] + 10, c[1])
        return float(angle_deg(a, b)) / 10

    def _sky_mask(self, horizon: list[float] | None) -> np.ndarray:
        """Pixels that see the sky: above the ground mask (or 3°), inside the lens."""
        h, w = self.cam.height, self.cam.width
        v, u = np.mgrid[0:h, 0:w].astype(np.float32)
        d = self.cam.pixel_to_world(u.ravel(), v.ravel())
        ok = np.isfinite(d).all(axis=1)
        az, alt = vec_to_altaz(np.where(ok[:, None], d, [0, 0, 1]))
        limit = np.full(az.shape, 3.0)
        if horizon and len(horizon) == 360:
            limit = np.maximum(limit, np.asarray(horizon)[np.floor(az).astype(int) % 360] + 1.0)
        return (ok & (alt > limit)).reshape(h, w)

    def _fixed_lights(self, t: float, sun_alt: float, bodies: list[dict]) -> np.ndarray:
        """Discs over the bright stars (at night), the planets, the Moon and the Sun."""
        import cv2

        m = np.zeros((self.cam.height, self.cam.width), np.uint8)
        pts = []
        if sun_alt < -6 and self.star_dirs is not None:
            bright = self.star_mags < self.cfg.star_mag
            for d, mag in zip(self.star_dirs[bright], self.star_mags[bright]):
                pts.append((d, 2 + 1.5 * (self.cfg.star_mag - mag)))
        for b in bodies:
            r_deg = {"sun": 12.0, "moon": 3.0}.get(b["kind"], 0.4)
            pts.append((altaz_to_vec(b["az"], b["alt"]), r_deg / self.px_deg if b["kind"] != "planet" else 4))
        if not pts:
            return m.astype(bool)
        dirs = np.array([p[0] for p in pts])
        u, v, ok = self.cam.world_to_pixel(dirs)
        for (uu, vv, good, (_, r)) in zip(u, v, ok, pts):
            if good:
                cv2.circle(m, (int(round(uu)), int(round(vv))), int(math.ceil(r)), 1, -1)
        return m.astype(bool)

    def process(self, f: Frame, snapshot: dict | None = None) -> list[Track]:
        """One frame: detect, follow, and return the tracks that ended."""
        snap = snapshot if snapshot is not None else self.model.snapshot(
            datetime.fromtimestamp(f.t, timezone.utc), min_alt=-1)
        sun_alt = snap["sun_alt"]
        dets = self._detect(f, snap)
        self._follow(dets, f, snap)
        done = [tr for tr in self.tracks
                if f.t - tr.last.t > max(self.cfg.end_after_s, 3 * (f.exposure_s + 0.2), 2.5 * f.exposure_s + 1)]
        self.tracks = [tr for tr in self.tracks if tr not in done]
        return [tr for tr in done if self._counts(tr)]

    def flush(self) -> list[Track]:
        done, self.tracks = self.tracks, []
        return [tr for tr in done if self._counts(tr)]

    def _counts(self, tr: Track) -> bool:
        return any(d.streak for d in tr.dets) or len(tr.dets) >= self.cfg.min_frames_day

    # -- detection --

    def _detect(self, f: Frame, snap: dict) -> list[Detection]:
        import cv2

        img = f.image
        reset = (self.bg is None or self.bg.shape != img.shape or self.last is None
                 or abs(math.log((f.exposure_s * 10 ** (f.gain / 200) + 1e-9)
                                 / (self.last.exposure_s * 10 ** (self.last.gain / 200) + 1e-9))) > 0.3)
        if reset:   # the first frame, or the exposure jumped: start the background again
            self.bg = img.copy()
            self.last = f
            return []
        diff = img - self.bg
        sky = self._sky & ~self._fixed_lights(f.t, snap["sun_alt"], snap["bodies"])
        sample = diff[sky][:: max(1, int(sky.sum() // 200000))]
        noise = 1.4826 * float(np.median(np.abs(sample - np.median(sample)))) + 0.05
        hot = (diff > self.cfg.sigma * noise) & sky
        hot = cv2.morphologyEx(hot.astype(np.uint8), cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
        if f.exposure_s >= self.cfg.streak_exposure_s:   # join a blinking aircraft's dots into one streak
            hot = cv2.morphologyEx(hot, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        n, lab, stats, cent = cv2.connectedComponentsWithStats(hot, connectivity=8)
        # The background follows the sky slowly, and not where something's passing
        alpha = 0.3 if f.exposure_s >= 1 else 0.1
        upd = cv2.dilate(hot, np.ones((7, 7), np.uint8)) == 0
        self.bg[upd] += alpha * (img[upd] - self.bg[upd])
        self.last = f
        if n - 1 > self.cfg.max_blobs:
            self.skipped += 1
            return []
        out = []
        for i in range(1, n):
            area = int(stats[i, cv2.CC_STAT_AREA])
            if area < self.cfg.min_area:
                continue
            x0, y0, w, h = (int(stats[i, k]) for k in (cv2.CC_STAT_LEFT, cv2.CC_STAT_TOP, cv2.CC_STAT_WIDTH,
                                                          cv2.CC_STAT_HEIGHT))
            ys, xs = np.nonzero(lab[y0:y0 + h, x0:x0 + w] == i)
            wts = diff[y0 + ys, x0 + xs].clip(min=0)
            flux = float(wts.sum())
            cu, cv_ = float(np.average(x0 + xs, weights=wts + 1e-6)), float(np.average(y0 + ys, weights=wts + 1e-6))
            cov = np.cov(np.stack([xs, ys]), aweights=wts + 1e-6) if len(xs) > 2 else np.eye(2) * 0.25
            ev, evec = np.linalg.eigh(cov)
            major, minor = 4 * math.sqrt(max(ev[1], 0.0625)), 4 * math.sqrt(max(ev[0], 0.0625))
            streak = (f.exposure_s >= self.cfg.streak_exposure_s and major >= self.cfg.streak_min_px
                      and major / minor >= self.cfg.streak_elong)
            d = self.cam.pixel_to_world(np.array([cu]), np.array([cv_]))[0]
            if not np.all(np.isfinite(d)):
                continue
            az, alt = vec_to_altaz(d)
            ends = None
            if streak:
                ax = evec[:, 1] * major / 2
                e = self.cam.pixel_to_world(np.array([cu - ax[0], cu + ax[0]]), np.array([cv_ - ax[1], cv_ + ax[1]]))
                ea, el = vec_to_altaz(e)
                ends = ((float(ea[0]), float(el[0])), (float(ea[1]), float(el[1])))
            pad = int(max(12, major))
            cy0, cy1 = max(0, int(cv_) - pad), min(img.shape[0], int(cv_) + pad + 1)
            cx0, cx1 = max(0, int(cu) - pad), min(img.shape[1], int(cu) + pad + 1)
            out.append(Detection(f.t, f.exposure_s, cu, cv_, float(az), float(alt), area, flux,
                                 float(diff[y0 + ys, x0 + xs].max()), major * self.px_deg, minor * self.px_deg,
                                 bool(streak), ends, img[cy0:cy1, cx0:cx1].copy()))
        return out

    # -- tracking and matching --

    def _follow(self, dets: list[Detection], f: Frame, snap: dict) -> None:
        free = list(range(len(dets)))
        for tr in sorted(self.tracks, key=lambda t: -len(t.dets)):
            if not free:
                break
            p = tr.predict(f.t)
            dirs = altaz_to_vec(np.array([dets[i].az for i in free]), np.array([dets[i].alt for i in free]))
            dist = angle_deg(dirs, p)
            dt = f.t - tr.last.t
            if len(tr.dets) == 1:   # no speed yet: anything it could have flown since
                gate = self.cfg.gate_deg + self.cfg.first_step_dps * dt
            else:
                gate = self.cfg.gate_deg + 0.5 * float(np.linalg.norm(tr.velocity())) * 57.3 * dt
            if tr.last.streak:
                gate += tr.last.length_deg
            k = int(np.argmin(dist))
            if dist[k] <= gate:
                tr.dets.append(dets[free.pop(k)])
        for i in free:
            self.tracks.append(Track(self.next_id, [dets[i]]))
            self.next_id += 1
        now_dets = {id(d) for d in dets}
        for tr in self.tracks:
            if id(tr.last) in now_dets:
                self._vote(tr, tr.last, f, snap)

    def _vote(self, tr: Track, d: Detection, f: Frame, snap: dict) -> None:
        here = altaz_to_vec(d.az, d.alt)
        planes = snap.get("planes") or []
        if planes and self.model._adsb is not None:
            shift = f.t - self.model._adsb.fetched_at
            pl = aircraft_look(planes, self.model.lat, self.model.lon, self.model.elev_m, shift_s=shift) \
                if "age_s" in planes[0] else planes
            for p in pl:
                gate = self.cfg.adsb_gate_deg + (d.length_deg / 2 if d.streak else 0)
                if float(angle_deg(here, altaz_to_vec(p["az"], p["alt"]))) <= gate:
                    v = tr.adsb.setdefault(p["hex"], [0, p.get("flight") or p["hex"].upper(), {}])
                    v[0] += 1
                    v[2] = {"alt_ft": p.get("alt_ft"), "range_km": round(p.get("range_km", 0), 1),
                            "speed_kt": p.get("speed_kt"), "hex": p["hex"], "category": p.get("category", "")}
        sats = self.model.satellites()
        if sats is not None and snap["sun_alt"] < -4:
            t = datetime.fromtimestamp(f.t, timezone.utc)
            look = sats.look(self.model.lat, self.model.lon, self.model.elev_m, t)
            up = look["ok"] & look["sunlit"] & (look["alt"] > 0)
            if up.any():
                dirs = altaz_to_vec(look["az"][up], look["alt"][up])
                gate = self.cfg.sat_gate_deg + (d.length_deg / 2 if d.streak else 0)
                for j in np.nonzero(angle_deg(dirs, here) <= gate)[0]:
                    i = int(np.nonzero(up)[0][j])
                    v = tr.sats.setdefault(int(sats.norad[i]), [0, sats.names[i], {}])
                    v[0] += 1
                    v[2] = {"range_km": round(float(look["range_km"][i])), "norad": int(sats.norad[i])}

    def identify(self, tr: Track, sun_alt: float) -> dict:
        """What it was: the record for the log (without pictures)."""
        n = len(tr.dets)
        feats = track_features(tr, sun_alt)
        rec = {"start": tr.dets[0].t, "end": tr.last.t + tr.last.exposure_s, "frames": n, "sun_alt": sun_alt,
               "features": feats, "match": None, "match_id": None, "detail": None, "shower": None,
               "path": [[round(d.t, 2), round(d.az, 3), round(d.alt, 3)] for d in tr.dets]}

        def best(votes: dict):
            if not votes:
                return None
            k = max(votes, key=lambda k: votes[k][0])
            return (k, *votes[k]) if votes[k][0] >= max(1, 0.5 * n) else None

        plane, sat = best(tr.adsb), best(tr.sats)
        if plane and (not sat or plane[1] >= sat[1]):
            kind, conf, src = ("helicopter" if plane[3].get("category") == "A7" else "plane"), 0.95, "adsb"
            rec.update(match=plane[2], match_id=plane[0], detail=plane[3])
        elif sat:
            kind, conf, src = "satellite", 0.9, "tle"
            rec.update(match=sat[2], match_id=str(sat[0]), detail=sat[3])
        else:
            kind, conf, src = self.classifier.classify(feats)
        if kind == "meteor" and tr.dets[0].ends:
            rec["shower"] = shower_of(tr.dets[0].ends, tr.dets[0].t, self.model.lat, self.model.lon)
        rec.update(kind=kind, auto_kind=kind, confidence=round(conf, 2), source=src,
                   unknown=int(kind == "unknown"))
        return rec


# ---- pictures for the log ---------------------------------------------------------------------

def _to8(crop: np.ndarray) -> np.ndarray:
    lo, hi = np.percentile(crop, 5), np.percentile(crop, 99.8)
    return (np.clip((crop - lo) / max(hi - lo, 1e-3), 0, 1) ** 0.6 * 255).astype(np.uint8)


def save_media(tr: Track, folder: Path, oid: int) -> tuple[str | None, str | None]:
    """A thumbnail of its brightest moment and, with more than one frame, a
    small animation; file names relative to the media folder."""
    from PIL import Image

    crops = [d.crop for d in tr.dets if d.crop is not None and d.crop.size]
    if not crops:
        return None, None
    day = datetime.fromtimestamp(tr.dets[0].t, timezone.utc).strftime("%Y-%m-%d")
    out = folder / day
    out.mkdir(parents=True, exist_ok=True)
    k = int(np.argmax([d.flux for d in tr.dets if d.crop is not None and d.crop.size]))
    thumb = Image.fromarray(_to8(crops[k])).resize((160, 160), Image.NEAREST if crops[k].shape[0] < 80 else Image.BILINEAR)
    thumb.save(out / f"{oid}.jpg", quality=88)
    clip = None
    if len(crops) > 1:
        step = max(1, len(crops) // 40)
        frames = [Image.fromarray(_to8(c)).resize((160, 160)) for c in crops[::step]]
        frames[0].save(out / f"{oid}.gif", save_all=True, append_images=frames[1:], duration=200, loop=0)
        clip = f"{day}/{oid}.gif"
    return f"{day}/{oid}.jpg", clip


def log_track(db: sqlite3.Connection, rec: dict, tr: Track, media: Path) -> int:
    cur = db.execute(
        "INSERT INTO objects (start, end, frames, kind, auto_kind, confidence, source, match, match_id, detail, "
        "shower, unknown, sun_alt, path, features) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rec["start"], rec["end"], rec["frames"], rec["kind"], rec["auto_kind"], rec["confidence"], rec["source"],
         rec["match"], rec["match_id"], json.dumps(rec["detail"]) if rec["detail"] else None, rec["shower"],
         rec["unknown"], rec["sun_alt"], json.dumps(rec["path"]), json.dumps(rec["features"])))
    oid = int(cur.lastrowid)
    thumb, clip = save_media(tr, media, oid)
    db.execute("UPDATE objects SET thumb = ?, clip = ? WHERE id = ?", (thumb, clip, oid))
    db.commit()
    return oid
