"""SkyDome pipeline: geometry, camera models, dome projections and stitch LUTs.

Conventions (DECISIONS.md D-003):
  * World frame is local ENU: x = east, y = north, z = up.
  * Azimuth is measured from north toward east; altitude above the horizon.
  * Camera frame follows OpenCV: x = right, y = down, z = forward.
  * A camera's rotation ``R`` is world-from-camera: its columns are the
    camera's x, y, z axes expressed in ENU.
"""
