"""Stars: where they are, where the cameras see them, and how each camera
must point (and its lens behave) for the two to agree (DECISIONS.md D-027).

Catalogue: the Yale Bright Star Catalogue (BSC5, CDS V/50, ~9100 stars to
V 6.5). Positions are J2000; they're precessed to the date (~22' since
2000, more than ten pixels here), turned into altitude/azimuth for the site
and lifted by refraction. Nutation and aberration (< 1') are left out.
"""

from __future__ import annotations

import gzip
import itertools
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .camera import Camera, FisheyeLens
from .geometry import altaz_to_vec

# The same file from SAO first: CDS didn't answer from the rig on 2026-11-03
BSC_URLS = ("http://tdc-www.harvard.edu/catalogs/bsc5.dat.gz", "https://cdsarc.cds.unistra.fr/ftp/V/50/catalog.gz")


# ---- catalogue -------------------------------------------------------------------

@dataclass(frozen=True)
class Catalogue:
    ra_deg: np.ndarray    # J2000
    dec_deg: np.ndarray
    vmag: np.ndarray
    hr: np.ndarray | None = None          # Harvard Revised number
    names: tuple[str, ...] = ()           # BSC5 columns 5-14 ("  3Alp Lyr"; star_names.describe)


def load_bsc(cache: Path) -> Catalogue:
    """BSC5, downloaded once into `cache`."""
    cache = Path(cache)
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        for url in BSC_URLS:
            try:
                with urllib.request.urlopen(url, timeout=60) as r:
                    cache.write_bytes(gzip.decompress(r.read()))
                break
            except OSError:
                continue
        else:
            raise OSError(f"couldn't download the star catalogue; put it in {cache} by hand")
    return parse_bsc(cache.read_text(errors="replace"))


def parse_bsc(text: str) -> Catalogue:
    ra, dec, mag, hr, names = [], [], [], [], []
    for line in text.splitlines():
        if len(line) < 107 or not line[75:77].strip() or not line[102:107].strip():
            continue   # a few entries (novae, clusters) have no position or magnitude
        h, m, s = int(line[75:77]), int(line[77:79]), float(line[79:83])
        sign = -1.0 if line[83] == "-" else 1.0
        dd, dm, ds = int(line[84:86]), int(line[86:88]), int(line[88:90])
        ra.append(15.0 * (h + m / 60 + s / 3600))
        dec.append(sign * (dd + dm / 60 + ds / 3600))
        mag.append(float(line[102:107]))
        hr.append(int(line[0:4]))
        names.append(line[4:14])
    return Catalogue(np.array(ra), np.array(dec), np.array(mag), np.array(hr), tuple(names))


# ---- apparent positions ---------------------------------------------------------

def julian_date(t: datetime) -> float:
    return t.timestamp() / 86400.0 + 2440587.5


def _precession(jd: float) -> np.ndarray:
    """J2000 -> mean equator of date (IAU 1976 angles)."""
    T = (jd - 2451545.0) / 36525.0
    zeta = np.radians((2306.2181 * T + 0.30188 * T**2 + 0.017998 * T**3) / 3600)
    z = np.radians((2306.2181 * T + 1.09468 * T**2 + 0.018203 * T**3) / 3600)
    th = np.radians((2004.3109 * T - 0.42665 * T**2 - 0.041833 * T**3) / 3600)

    def rz(a):
        c, s = np.cos(a), np.sin(a)
        return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])

    def ry(a):
        c, s = np.cos(a), np.sin(a)
        return np.array([[c, 0, -s], [0, 1, 0], [s, 0, c]])

    return rz(z) @ ry(th) @ rz(zeta)


def refraction_deg(alt_deg: np.ndarray) -> np.ndarray:
    """Bennett's formula, standard air; how much higher a star appears."""
    h = np.maximum(alt_deg, -1.0)
    return 1.0 / np.tan(np.radians(h + 7.31 / (h + 4.4))) / 60.0


def apparent_altaz(cat: Catalogue, lat_deg: float, lon_deg: float, t: datetime) -> tuple[np.ndarray, np.ndarray]:
    """Azimuth (from north through east) and refracted altitude, degrees."""
    jd = julian_date(t)
    ra, dec = np.radians(cat.ra_deg), np.radians(cat.dec_deg)
    v = np.stack([np.cos(dec) * np.cos(ra), np.cos(dec) * np.sin(ra), np.sin(dec)], axis=-1) @ _precession(jd).T
    ra_d = np.arctan2(v[:, 1], v[:, 0])
    dec_d = np.arcsin(np.clip(v[:, 2], -1, 1))
    az, alt = _altaz_of_date(ra_d, dec_d, lat_deg, lon_deg, jd)
    return az, alt + refraction_deg(alt)


def sun_altaz(lat_deg: float, lon_deg: float, t: datetime) -> tuple[float, float]:
    """The Sun's azimuth and altitude (no refraction), to ~0.01°: enough to
    tell day, twilight and night apart (Astronomical Almanac low-precision
    formulae)."""
    jd = julian_date(t)
    n = jd - 2451545.0
    L = np.radians((280.460 + 0.9856474 * n) % 360)
    g = np.radians((357.528 + 0.9856003 * n) % 360)
    lam = L + np.radians(1.915) * np.sin(g) + np.radians(0.020) * np.sin(2 * g)
    eps = np.radians(23.439 - 0.0000004 * n)
    ra = np.arctan2(np.cos(eps) * np.sin(lam), np.cos(lam))
    dec = np.arcsin(np.sin(eps) * np.sin(lam))
    az, alt = _altaz_of_date(np.array([ra]), np.array([dec]), lat_deg, lon_deg, jd)
    return float(az[0]), float(alt[0])


def _altaz_of_date(ra_d: np.ndarray, dec_d: np.ndarray, lat_deg: float, lon_deg: float, jd: float):
    """Right ascension / declination of date (radians) -> azimuth, altitude (degrees, unrefracted)."""
    T = (jd - 2451545.0) / 36525.0
    gmst = 280.46061837 + 360.98564736629 * (jd - 2451545.0) + 0.000387933 * T**2
    H = np.radians(gmst + lon_deg) - ra_d
    phi = np.radians(lat_deg)
    alt = np.degrees(np.arcsin(np.sin(phi) * np.sin(dec_d) + np.cos(phi) * np.cos(dec_d) * np.cos(H)))
    az = np.degrees(np.arctan2(-np.cos(dec_d) * np.sin(H),
                               np.sin(dec_d) * np.cos(phi) - np.cos(dec_d) * np.cos(H) * np.sin(phi))) % 360
    return az, alt


# ---- stars in a frame -----------------------------------------------------------

@dataclass(frozen=True)
class Detections:
    u: np.ndarray       # sub-pixel centroids
    v: np.ndarray
    flux: np.ndarray    # sum above the local background, 5x5
    snr: np.ndarray     # peak over the noise


def _box(y: np.ndarray, k: int) -> np.ndarray:
    pad = np.pad(y, k // 2, mode="edge")
    c = np.cumsum(np.cumsum(pad, 0), 1)
    c = np.pad(c, ((1, 0), (1, 0)))
    return (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / (k * k)


def sky_blocks(cam, block: int = 48, min_alt_deg: float = 5.0) -> np.ndarray:
    """For sky_stars(up=...): the blocks of cam's picture whose centre looks
    at least min_alt_deg above the horizon."""
    hb, wb = cam.height // block, cam.width // block
    v, u = np.mgrid[0:hb, 0:wb]
    d = cam.pixel_to_world((u.ravel() + 0.5) * block, (v.ravel() + 0.5) * block)
    ok = np.isfinite(d).all(axis=1) & (d[:, 2] > np.sin(np.radians(min_alt_deg)))
    return ok.reshape(hb, wb)


def detect_stars(y: np.ndarray, sigma: float = 6.0, max_stars: int = 400, border: int = 12) -> Detections:
    """Point sources in a grey frame (stars, and hot pixels the matching ignores)."""
    y = y.astype(np.float32)
    hp = y - _box(y, 15)
    noise = max(1.4826 * float(np.median(np.abs(hp - np.median(hp)))), 0.3)
    c = hp[2:-2, 2:-2]
    peak = np.ones_like(c, bool)
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            if dy or dx:
                peak &= c >= hp[2 + dy:hp.shape[0] - 2 + dy, 2 + dx:hp.shape[1] - 2 + dx]
    rr, cc = np.nonzero(peak & (c > sigma * noise) & (c > 3))
    rr, cc = rr + 2, cc + 2
    keep = (rr > border) & (rr < y.shape[0] - border) & (cc > border) & (cc < y.shape[1] - border)
    rr, cc = rr[keep], cc[keep]
    order = np.argsort(hp[rr, cc])[::-1][:max_stars]
    rr, cc = rr[order], cc[order]
    us, vs, fl, sn = [], [], [], []
    yy, xx = np.mgrid[-2:3, -2:3]
    for r, q in zip(rr, cc):
        w = np.clip(hp[r - 2:r + 3, q - 2:q + 3], 0, None)
        s = w.sum()
        if s <= 0:
            continue
        us.append(q + (w * xx).sum() / s)
        vs.append(r + (w * yy).sum() / s)
        fl.append(s)
        sn.append(hp[r, q] / noise)
    return Detections(np.array(us), np.array(vs), np.array(fl), np.array(sn))


# ---- solving a camera --------------------------------------------------------------

def _rotvec(w: np.ndarray) -> np.ndarray:
    th = np.linalg.norm(w)
    if th < 1e-12:
        return np.eye(3)
    k = w / th
    K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + np.sin(th) * K + (1 - np.cos(th)) * K @ K


@dataclass
class Solution:
    camera: Camera
    matches: int
    rms_px: float
    rms_arcmin: float
    fit_lens: bool


def _model(cam: Camera, p: np.ndarray) -> Camera:
    """cam turned by p[0:3] (a small rotation in the camera's frame), its lens
    scaled by 1 + p[3], k1 + p[4], centre moved by p[5:7]"""
    lens = cam.lens
    s = 1.0 + p[3]
    lens = FisheyeLens(lens.width, lens.height, lens.fx * s, lens.fy * s, lens.cx + p[5], lens.cy + p[6],
                       (lens.k[0] + p[4], *lens.k[1:]), lens.max_theta_deg)
    return Camera(cam.name, lens, cam.rotation @ _rotvec(p[:3]))


def _project(cam: Camera, dirs: np.ndarray):
    u, v, ok = cam.world_to_pixel(dirs)
    return u, v, ok


def _match(du, dv, cu, cv, tol):
    """Unique nearest pairs (detection i, catalogue j) closer than tol pixels."""
    if len(du) == 0 or len(cu) == 0:
        return np.array([], int), np.array([], int)
    d2 = (du[:, None] - cu[None, :]) ** 2 + (dv[:, None] - cv[None, :]) ** 2
    j = np.argmin(d2, axis=1)
    i = np.arange(len(du))
    ok = d2[i, j] < tol * tol
    i, j = i[ok], j[ok]
    best = {}
    for a, b in zip(i, j):   # one detection per catalogue star: the closest
        if b not in best or d2[a, b] < d2[best[b], b]:
            best[b] = a
    jj = np.array(sorted(best), int)
    return np.array([best[b] for b in jj], int), jj


def sky_stars(y: np.ndarray, det: Detections, bottom: float = 0.8, block: int = 48, glare: float = 15.0,
              inner: int = 2, outer: int = 9, min_core: float = 0.7, up: np.ndarray | None = None) -> Detections:
    """The detections that are stars: on dark sky (not glare, not the ground)
    and point-like (the light in a small core on a quiet surround, unlike lit
    windows, trees and wires). The ground: the bottom of the frame (a side
    camera), or, given `up` (one flag per block: it sees the sky), outside
    those blocks (a fisheye, whose horizon is a circle and corners are dark)."""
    y = y.astype(np.float32)
    H, W = y.shape
    hb, wb = H // block, W // block
    bg = np.median(y[:hb * block, :wb * block].reshape(hb, block, wb, block).transpose(0, 2, 1, 3)
                   .reshape(hb, wb, -1), axis=2)
    bi = np.clip((det.v // block).astype(int), 0, hb - 1)
    bj = np.clip((det.u // block).astype(int), 0, wb - 1)
    if up is None:
        sky = np.percentile(bg, 25)
        keep = (bg[bi, bj] < sky + glare) & (det.v < bottom * H)
    else:
        up = np.asarray(up, bool)[:hb, :wb]
        sky = np.percentile(bg[up], 25) if up.any() else np.percentile(bg, 25)
        keep = (bg[bi, bj] < sky + glare) & up[bi, bj]
    yy, xx = np.mgrid[-outer:outer + 1, -outer:outer + 1]
    rr = np.hypot(xx, yy)
    core, ring = rr <= inner + 0.5, rr > outer - 2.5
    for k in np.nonzero(keep)[0]:
        x, v = int(round(det.u[k])), int(round(det.v[k]))
        if x < outer or v < outer or x >= W - outer or v >= H - outer:
            keep[k] = False
            continue
        p = y[v - outer:v + outer + 1, x - outer:x + outer + 1]
        b = np.median(p[ring])
        noise = 1.4826 * np.median(np.abs(p[ring] - b)) + 0.5
        c, tot = (p[core] - b).sum(), (p[~ring] - b).sum()
        keep[k] = c > 0 and c / max(tot, 1e-3) > min_core and np.percentile(np.abs(p[ring] - b), 90) < 4 * noise + 2
    return Detections(det.u[keep], det.v[keep], det.flux[keep], det.snr[keep])


def _triangles(P: np.ndarray, min_side: float) -> tuple[np.ndarray, np.ndarray]:
    """Every triangle of the unit vectors P: vertex indices (ordered by the
    side opposite, shortest first) and the sides in degrees."""
    idx, sides = [], []
    for a, b, c in itertools.combinations(range(len(P)), 3):
        sd = np.degrees(np.arccos(np.clip([P[b] @ P[c], P[a] @ P[c], P[a] @ P[b]], -1, 1)))
        if sd.min() < min_side:
            continue
        o = np.argsort(sd)
        idx.append(np.array((a, b, c))[o])
        sides.append(sd[o])
    return np.array(idx).reshape(-1, 3), np.array(sides).reshape(-1, 3)


def _kabsch(A: np.ndarray, B: np.ndarray) -> np.ndarray:
    """The rotation R with R @ A[i] ~ B[i]."""
    U, _, Vt = np.linalg.svd(A.T @ B)
    d = np.sign(np.linalg.det(Vt.T @ U.T))
    return Vt.T @ np.diag([1, 1, d]) @ U.T


def blind_pointing(cam: Camera, det: Detections, star_dirs: np.ndarray, star_mag: np.ndarray,
                   n_det: int = 22, n_cat: int = 90, scale_range: tuple[float, float] = (0.9, 1.12)) -> Camera | None:
    """How the camera points with no prior: triangles of its brightest stars
    matched by shape to triangles of the brightest catalogue stars (so the
    lens scale may be off by scale_range), each candidate scored by how many
    stars it puts on detections."""
    if len(det.u) < 6:
        return None
    top = np.argsort(det.flux)[::-1]
    du, dv = det.u[top[:n_det]], det.v[top[:n_det]]
    rays = cam.lens.unproject(du, dv)
    C = star_dirs[np.argsort(star_mag)[:n_cat]]
    di, ds = _triangles(rays, 5.0)
    ci, cs = _triangles(C, 4.0)
    cshape = cs[:, :2] / cs[:, 2:]
    su, sv = det.u[top[:150]], det.v[top[:150]]
    score_dirs = star_dirs[star_mag < 4.5]
    best, best_cam = 6, None
    for ii, sd in zip(di, ds):
        ratio = cs[:, 2] / sd[2]
        for h in np.nonzero((np.abs(cshape - sd[:2] / sd[2]).max(1) < 0.012)
                            & (ratio > scale_range[0]) & (ratio < scale_range[1]))[0]:
            k = ratio[h]
            lens = replace(cam.lens, fx=cam.lens.fx / k, fy=cam.lens.fy / k)
            R = _kabsch(lens.unproject(du[ii], dv[ii]), C[ci[h]])
            cand = Camera(cam.name, lens, R)
            cu, cv, ok = cand.world_to_pixel(score_dirs)
            i, _ = _match(su, sv, cu[ok], cv[ok], 12.0)
            if len(i) > best:
                best, best_cam = len(i), cand
    return best_cam


def _gauss_newton(cam: Camera, p: np.ndarray, npar: int, mu, mv, md) -> np.ndarray:
    """Pixel residuals of the matched stars, minimised over the first npar of p."""
    for _ in range(20):
        def res(q):
            full = p.copy()
            full[:npar] = q
            u, v, _ = _project(_model(cam, full), md)
            return np.concatenate([u - mu, v - mv])
        q = p[:npar].copy()
        r0 = res(q)
        J = np.empty((len(r0), npar))
        for k in range(npar):
            h = 1e-7 if k in (3, 4) else 1e-6
            dq = q.copy()
            dq[k] += h
            J[:, k] = (res(dq) - r0) / h
        step = np.linalg.lstsq(J, -r0, rcond=None)[0]
        p[:npar] = q + step
        if np.abs(step).max() < 1e-9:
            break
    return p


def refine_camera(cam: Camera, det: Detections, star_dirs: np.ndarray, star_mag: np.ndarray,
                  fit_lens_min: int = 25) -> Solution | None:
    """From a pointing within a degree or two: match catalogue stars to the
    nearest detections, tighter and fainter stage by stage, fitting rotation,
    then lens scale, then centre and distortion as the matches allow, and
    dropping pairs far off the fit (stray detections near a star)."""
    p = np.zeros(7)
    out = None
    for tol, maglim in ((45, 3.5), (30, 4.0), (20, 4.5), (12, 5.0), (8, 5.5), (5, 6.0), (4, 6.5), (4, 6.5)):
        sel = star_mag < maglim
        cu, cv, ok = _project(_model(cam, p), star_dirs[sel])
        ok &= (cu > 5) & (cv > 5) & (cu < cam.width - 5) & (cv < cam.height - 5)
        idx = np.nonzero(sel)[0][ok]
        i, j = _match(det.u, det.v, cu[ok], cv[ok], tol)
        if len(i) < 6:
            return out
        mu, mv, md = det.u[i], det.v[i], star_dirs[idx[j]]
        npar = 7 if len(i) >= fit_lens_min else 4 if len(i) >= 10 else 3
        for _ in range(3):
            p = _gauss_newton(cam, p, npar, mu, mv, md)
            u, v, _ = _project(_model(cam, p), md)
            r = np.hypot(u - mu, v - mv)
            keep = r < max(3 * np.median(r), 1.5)
            mu, mv, md = mu[keep], mv[keep], md[keep]
        final = _model(cam, p)
        got = final.pixel_to_world(mu, mv)
        ang = np.degrees(np.arccos(np.clip(np.sum(got * md, axis=1), -1, 1))) * 60
        out = Solution(final, int(keep.sum()), float(np.sqrt(np.mean(r[keep] ** 2))),
                       float(np.sqrt(np.mean(ang ** 2))), npar == 7)
    return out


def solve_camera(cam: Camera, det: Detections, star_dirs: np.ndarray, star_mag: np.ndarray,
                 good: int = 30) -> Solution | None:
    """Pointing (and, with enough stars, lens scale/centre/k1) that puts the
    catalogue on the detections: refined from where the camera is thought to
    point, or found blind when that doesn't fit (a camera turned a quarter
    turn, a new lens). None if the stars can't be matched."""
    def better(a: Solution | None, b: Solution | None) -> Solution | None:
        if a is None or b is None:
            return a or b
        return max(a, b, key=lambda s: (s.rms_px < 1.5, s.matches))

    sol = refine_camera(cam, det, star_dirs, star_mag)
    if sol is not None and sol.matches >= good and sol.rms_px < 1.5:
        return sol
    start = blind_pointing(cam, det, star_dirs, star_mag)
    if start is not None:
        sol = better(sol, refine_camera(start, det, star_dirs, star_mag))
    return sol if sol is not None and sol.matches >= 10 else None


@dataclass
class RigSolution:
    """The rig as found in the sky: each camera's camera (pointing + lens)
    and how it was found."""
    cameras: dict[str, Camera]
    own: dict[str, Solution]   # cameras whose own stars fitted them
    turn: np.ndarray           # world-from-world rotation: configured rig -> real rig
    anchor: str                # the camera that found the rig


def solve_rig(cams: list[Camera], dets: dict[str, Detections], star_dirs: np.ndarray, star_mag: np.ndarray,
              good: int = 30, min_own: int = 15) -> RigSolution | None:
    """The whole rig at once, for a rig that has been moved or turned: the
    camera with the most stars is solved (blind if need be), which says how
    the rig turned; every other camera starts from its configured place on
    the rig turned the same way and is refined on its own stars. A camera
    that doesn't fit there (moved on the rig, a new lens) is solved blind;
    one with too few stars (cloud, trees) just turns with the rig."""
    order = sorted(cams, key=lambda c: -len(dets[c.name].u) if c.name in dets else 0)
    own: dict[str, Solution] = {}
    turn = anchor = None
    for c in order:
        if c.name not in dets:
            continue
        s = solve_camera(c, dets[c.name], star_dirs, star_mag, good)
        if s is not None and s.matches >= good and s.rms_px < 1.5:
            turn, anchor = s.camera.rotation @ c.rotation.T, c.name
            own[c.name] = s
            break
    if turn is None:
        return None
    out = {}
    for c in cams:
        if c.name in own:
            out[c.name] = own[c.name].camera
            continue
        seed = Camera(c.name, c.lens, turn @ c.rotation)
        out[c.name] = seed
        if c.name not in dets:
            continue
        s = refine_camera(seed, dets[c.name], star_dirs, star_mag)
        if s is None or s.matches < min_own or s.rms_px >= 1.5:
            s = solve_camera(seed, dets[c.name], star_dirs, star_mag, good)
        if s is not None and s.matches >= min_own and s.rms_px < 1.5:
            own[c.name] = s
            out[c.name] = s.camera
    return RigSolution(out, own, turn, anchor)


def star_directions(cat: Catalogue, lat: float, lon: float, t: datetime, min_alt: float = 8.0,
                    max_mag: float = 6.5) -> tuple[np.ndarray, np.ndarray]:
    az, alt = apparent_altaz(cat, lat, lon, t)
    keep = (alt > min_alt) & (cat.vmag < max_mag)
    return altaz_to_vec(az[keep], alt[keep]), cat.vmag[keep]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
