"""Camera orientations from what neighbouring cameras see in common
(handoff §5.3 step 4, "refine jointly ... overlap regions must agree").

Features matched between two overlapping cameras are, after unprojecting
through each lens, the same direction in the world: R_i a = R_j b for
world-from-camera rotations R_i, R_j and camera-frame rays a, b. So:

1. per pair, RANSAC a relative rotation from the matched rays (Kabsch);
2. chain those from a reference camera to get starting rotations;
3. refine all rotations together (robust Gauss-Newton on the ray
   mismatch), with the reference camera fixed;
4. place the whole set in the world frame: either as close as possible to
   the mount's nominal pointing (the rig), or with the cameras' mean view
   direction at the zenith (cameras loose on a bench).

This assumes the cameras share a centre (pure rotation). On the rig they
sit within ~10 cm of each other looking at the sky, so that holds; on a
bench looking at a room, parallax leaves some ghosting near the seams.
Absolute north/level still comes from the stars (Phase 2) or the mount.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np


def skew(v: np.ndarray) -> np.ndarray:
    x, y, z = v
    return np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])


def exp_so3(w: np.ndarray) -> np.ndarray:
    th = np.linalg.norm(w)
    if th < 1e-12:
        return np.eye(3) + skew(w)
    k = skew(w / th)
    return np.eye(3) + np.sin(th) * k + (1 - np.cos(th)) * k @ k


def kabsch(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Rotation Q minimising sum |a - Q b|^2 for unit rays (N, 3)."""
    h = b.T @ a
    u, _, vt = np.linalg.svd(h)
    d = np.sign(np.linalg.det(vt.T @ u.T))
    return vt.T @ np.diag([1, 1, d]) @ u.T


def angle_deg(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.degrees(np.arctan2(np.linalg.norm(np.cross(a, b), axis=-1), np.sum(a * b, axis=-1)))


@dataclass
class PairMatches:
    """Matched rays between cameras i and j (camera frames, unit vectors)."""

    i: int
    j: int
    a: np.ndarray   # (N, 3) rays in camera i
    b: np.ndarray   # (N, 3) rays in camera j
    q: np.ndarray | None = None          # relative rotation: a ≈ q b
    inliers: np.ndarray | None = None    # bool (N,)

    @property
    def n_inliers(self) -> int:
        return 0 if self.inliers is None else int(self.inliers.sum())


def ransac_rotation(pm: PairMatches, thresh_deg: float = 1.0, iters: int = 2000, seed: int = 0) -> PairMatches:
    """Fill pm.q and pm.inliers. Two rays fix a rotation; three keep it stable."""
    n = len(pm.a)
    if n < 3:
        pm.q, pm.inliers = None, np.zeros(n, bool)
        return pm
    rng = np.random.default_rng(seed)
    best = np.zeros(n, bool)
    for _ in range(iters):
        idx = rng.choice(n, 3, replace=False)
        q = kabsch(pm.a[idx], pm.b[idx])
        inl = angle_deg(pm.a, pm.b @ q.T) < thresh_deg
        if inl.sum() > best.sum():
            best = inl
    if best.sum() >= 3:
        q = kabsch(pm.a[best], pm.b[best])
        best = angle_deg(pm.a, pm.b @ q.T) < thresh_deg
        pm.q = kabsch(pm.a[best], pm.b[best]) if best.sum() >= 3 else q
    pm.inliers = best
    return pm


@dataclass
class Alignment:
    rotations: list[np.ndarray | None]       # world-from-camera, None = not connected
    residual_deg: list[float]                # RMS ray mismatch per camera over its inliers
    matches: list[int]                       # inlier matches per camera
    pairs: list[tuple[int, int, int]] = field(default_factory=list)   # (i, j, inliers) used


def solve(pairs: list[PairMatches], n_cams: int, ref: int | None = None, min_inliers: int = 15,
          iters: int = 30) -> Alignment:
    """Rotations for every camera connected to ``ref`` through pairs with
    enough inliers; ``ref`` (default: the best-connected camera) is identity."""
    good = [p for p in pairs if p.q is not None and p.n_inliers >= min_inliers]
    if ref is None:
        score = np.zeros(n_cams)
        for p in good:
            score[p.i] += p.n_inliers
            score[p.j] += p.n_inliers
        ref = int(np.argmax(score))

    # Chain relative rotations outward from the reference, strongest pairs first
    rot: list[np.ndarray | None] = [None] * n_cams
    rot[ref] = np.eye(3)
    queue = deque([ref])
    while queue:
        k = queue.popleft()
        for p in sorted(good, key=lambda p: -p.n_inliers):
            if p.i == k and rot[p.j] is None:
                rot[p.j] = rot[k] @ p.q            # R_i a = R_j b, a = q b  ->  R_j = R_i q
                queue.append(p.j)
            elif p.j == k and rot[p.i] is None:
                rot[p.i] = rot[k] @ p.q.T
                queue.append(p.i)

    free = [k for k in range(n_cams) if rot[k] is not None and k != ref]
    col = {k: 3 * n for n, k in enumerate(free)}
    used = [p for p in good if rot[p.i] is not None and rot[p.j] is not None]
    for _ in range(iters):
        if not free:
            break
        jtj = np.zeros((3 * len(free), 3 * len(free)))
        jte = np.zeros(3 * len(free))
        for p in used:
            a, b = p.a[p.inliers], p.b[p.inliers]
            ri, rj = rot[p.i], rot[p.j]
            e = a @ ri.T - b @ rj.T                      # (N, 3)
            # Huber weights on the angular error, 0.25° knee
            r = np.linalg.norm(e, axis=1)
            k_ = np.radians(0.25)
            w = np.where(r <= k_, 1.0, k_ / np.maximum(r, 1e-12))
            ji = -np.einsum("ab,nbc->nac", ri, np.stack([skew(x) for x in a]))   # d e / d w_i
            jj = np.einsum("ab,nbc->nac", rj, np.stack([skew(x) for x in b]))    # d e / d w_j
            blocks = [(p.i, ji), (p.j, jj)]
            for ka, ja in blocks:
                if ka not in col:
                    continue
                for kb, jb in blocks:
                    if kb not in col:
                        continue
                    jtj[col[ka]:col[ka] + 3, col[kb]:col[kb] + 3] += np.einsum("n,nij,nik->jk", w, ja, jb)
                jte[col[ka]:col[ka] + 3] += np.einsum("n,nij,ni->j", w, ja, e)
        step = -np.linalg.solve(jtj + 1e-9 * np.eye(len(jte)), jte)
        for k in free:
            rot[k] = rot[k] @ exp_so3(step[col[k]:col[k] + 3])
        if np.abs(step).max() < 1e-9:
            break

    sq = np.zeros(n_cams)
    cnt = np.zeros(n_cams, int)
    for p in used:
        a, b = p.a[p.inliers], p.b[p.inliers]
        err = np.radians(angle_deg(a @ rot[p.i].T, b @ rot[p.j].T))
        for k in (p.i, p.j):
            sq[k] += (err ** 2).sum()
            cnt[k] += len(err)
    resid = [float(np.degrees(np.sqrt(sq[k] / cnt[k]))) if cnt[k] else float("nan") for k in range(n_cams)]
    return Alignment(rot, resid, cnt.tolist(), [(p.i, p.j, p.n_inliers) for p in used])


def frame_to_nominal(rotations: list[np.ndarray | None], nominal: list[np.ndarray]) -> list[np.ndarray | None]:
    """Turn the whole solved set (one rotation G) to best match the nominal
    mount pointing: G = argmin sum |G R_k - N_k|^2 (orthogonal Procrustes)."""
    m = sum(n @ r.T for r, n in zip(rotations, nominal) if r is not None)
    u, _, vt = np.linalg.svd(m)
    g = u @ np.diag([1, 1, np.sign(np.linalg.det(u @ vt))]) @ vt
    return [None if r is None else g @ r for r in rotations]


def frame_centered(rotations: list[np.ndarray | None]) -> list[np.ndarray | None]:
    """Turn the set (smallest rotation) so the cameras' mean view direction
    points at the zenith: puts loose bench cameras in the middle of the dome."""
    axes = [r[:, 2] for r in rotations if r is not None]
    m = np.mean(axes, axis=0)
    m /= np.linalg.norm(m)
    z = np.array([0.0, 0.0, 1.0])
    v = np.cross(m, z)
    s, c = np.linalg.norm(v), float(m @ z)
    g = np.eye(3) if s < 1e-9 else exp_so3(v / s * np.arctan2(s, c))
    return [None if r is None else g @ r for r in rotations]


@dataclass
class PixelPairs:
    """Matched pixel coordinates between cameras i and j (before unprojection)."""

    i: int
    j: int
    pa: np.ndarray   # (N, 2) u, v in camera i
    pb: np.ndarray   # (N, 2) u, v in camera j


def truncated_cost(pairs: list[PairMatches], al: Alignment, thresh_deg: float) -> float:
    """Sum of min(err, thresh)^2 over every match of every solved pair: unlike
    an inlier RMS it can't improve just by throwing matches away."""
    cost = 0.0
    for p in pairs:
        ri, rj = al.rotations[p.i], al.rotations[p.j]
        if ri is None or rj is None:
            continue
        err = angle_deg(p.a @ ri.T, p.b @ rj.T)
        cost += float(np.sum(np.minimum(err, thresh_deg) ** 2))
    return cost


def align_pixels(pixel_pairs: list[PixelPairs], lenses: list, n_cams: int, thresh_deg: float = 1.0,
                 min_inliers: int = 15) -> tuple[Alignment, list[PairMatches]]:
    """Unproject through ``lenses`` (one per camera), RANSAC each pair, solve."""
    pairs = []
    for pp in pixel_pairs:
        a = lenses[pp.i].unproject(pp.pa[:, 0], pp.pa[:, 1])
        b = lenses[pp.j].unproject(pp.pb[:, 0], pp.pb[:, 1])
        pairs.append(ransac_rotation(PairMatches(pp.i, pp.j, a, b), thresh_deg=thresh_deg, iters=500))
    return solve(pairs, n_cams, min_inliers=min_inliers), pairs


def fit_shared_hfov(pixel_pairs: list[PixelPairs], make_lens, n_cams: int, lo: float, hi: float,
                    thresh_deg: float = 1.0, min_inliers: int = 15, tol: float = 0.1) -> float:
    """Field of view (degrees) of a lens shared by all cameras that makes the
    overlaps agree best: golden-section search of the truncated cost.
    ``make_lens(hfov)`` builds the lens."""
    def cost(h):
        lens = make_lens(h)
        al, pairs = align_pixels(pixel_pairs, [lens] * n_cams, n_cams, thresh_deg, min_inliers)
        return truncated_cost(pairs, al, thresh_deg)

    g = (np.sqrt(5) - 1) / 2
    a, b = lo, hi
    c, d = b - g * (b - a), a + g * (b - a)
    fc, fd = cost(c), cost(d)
    while b - a > tol:
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - g * (b - a)
            fc = cost(c)
        else:
            a, c, fc = c, d, fd
            d = a + g * (b - a)
            fd = cost(d)
    return (a + b) / 2
