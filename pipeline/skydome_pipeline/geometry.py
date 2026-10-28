"""Direction vectors and rotations in the local horizontal (ENU) frame."""

from __future__ import annotations

import numpy as np


def altaz_to_vec(az_deg, alt_deg) -> np.ndarray:
    """Unit ENU vectors for azimuth/altitude in degrees. Broadcasts; returns (..., 3)."""
    az = np.radians(az_deg)
    alt = np.radians(alt_deg)
    cos_alt = np.cos(alt)
    return np.stack(np.broadcast_arrays(cos_alt * np.sin(az), cos_alt * np.cos(az), np.sin(alt)), axis=-1)


def vec_to_altaz(v: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Azimuth in [0, 360) and altitude in degrees for ENU vectors (..., 3)."""
    x, y, z = v[..., 0], v[..., 1], v[..., 2]
    az = np.degrees(np.arctan2(x, y)) % 360.0
    alt = np.degrees(np.arctan2(z, np.hypot(x, y)))
    return az, alt


def rot_x(deg: float) -> np.ndarray:
    c, s = np.cos(np.radians(deg)), np.sin(np.radians(deg))
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])


def rot_y(deg: float) -> np.ndarray:
    c, s = np.cos(np.radians(deg)), np.sin(np.radians(deg))
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def rot_z(deg: float) -> np.ndarray:
    c, s = np.cos(np.radians(deg)), np.sin(np.radians(deg))
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


def camera_rotation(az_deg: float, alt_deg: float, roll_deg: float = 0.0) -> np.ndarray:
    """World-from-camera rotation for a camera aimed at (az, alt).

    With roll 0 the image rows are level with the horizon and image "up"
    points toward the zenith. For a camera aimed straight up, az sets which
    way image "right" points: right = the direction of (az + 90).
    Positive roll turns the camera clockwise as seen from behind it.
    """
    forward = altaz_to_vec(az_deg, alt_deg)
    az = np.radians(az_deg)
    right = np.array([np.cos(az), -np.sin(az), 0.0])
    down = np.cross(forward, right)
    rot = np.stack([right, down, forward], axis=1)
    if roll_deg:
        rot = rot @ rot_z(roll_deg)
    return rot


def angle_between_deg(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Angle in degrees between unit vectors, numerically safe for tiny angles."""
    cross = np.linalg.norm(np.cross(a, b), axis=-1)
    dot = np.sum(a * b, axis=-1)
    return np.degrees(np.arctan2(cross, dot))
