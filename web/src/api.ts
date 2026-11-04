// The server's API (server/skydome_server/app.py)

export type Mode = "detail" | "smooth";

export interface CameraStats {
  fps: number;
  state: string;
  restarts: number;
  damaged_dropped: number;
  // Brightness/colour matching to the neighbours (D-022): Y x gain, U/V shifted
  balance?: { gain: number; u: number; v: number };
  // The ZWO camera (D-029): what the newest frame was taken with
  camera?: string;
  exposure_us?: number;
  gain?: number; // 0.1 dB
  auto_exposure?: boolean;
  sky_level?: number; // the sky's median, 0..1 of full scale
  temperature_c?: number;
}

export interface LiveStats {
  output_fps: number;
  stitch_ms: number;
  stitch_ms_max: number;
  skipped: number;
  push_failed: number;
  encoder: string;
  encoder_restarts: number;
  cameras: Record<string, CameraStats>;
  balance_points?: number;
}

export interface Status {
  mode: Mode;
  zoom_path: string;
  edge_zenith_deg: number;
  ground_mask: { available: boolean; on: boolean };
  smooth_until: string | null;
  live: LiveStats | null;
  stream: { path: string; webrtc_port: number; ready: boolean | null; viewers: number | null };
  system: {
    temps_c: Record<string, number>;
    cpu_pct: number | null;
    memory: { used: number; total: number } | null;
    disk: { free: number; total: number } | null;
    wifi_dbm: number | null;
    uptime_s: number | null;
  };
}

export interface Stretch {
  black: number; // below this is black
  k: number;     // asinh strength; 0 = off
}

export interface Control {
  name: string;
  min: number;
  max: number;
  step: number;
  default: number;
  value: number;
  set: boolean;
}

// A star up now: name (proper name, else designation), designation, constellation, magnitude, azimuth, altitude
export type StarRow = [string, string, string, number, number, number];

export interface Sky {
  utc: string;
  sun_alt: number;
  catalogue: boolean;
  stars: StarRow[];
}

export interface CameraFit {
  stars: number;
  rms_px: number | null;
  az_deg: number;
  alt_deg: number;
  moved_deg: number;
  own: boolean;
  lens: boolean;
}

export interface Calibration {
  state: "never" | "running" | "done" | "failed";
  message?: string;
  started_utc?: string;
  utc?: string;
  why?: string;
  applied?: boolean;
  anchor?: string;
  heading_deg?: number;
  tilt_deg?: number;
  stars?: number;
  cameras?: Record<string, CameraFit>;
  auto: boolean;
  sun_alt: number;
  dark_enough: boolean;
}

async function json<T>(r: Response): Promise<T> {
  if (!r.ok) {
    const text = await r.text();
    let detail = text;
    try {
      detail = JSON.parse(text).detail ?? text;   // FastAPI's {"detail": "..."}
    } catch { /* plain text */ }
    throw new Error(detail || `${r.status}`);
  }
  return r.json();
}

export const api = {
  status: () => fetch("/api/status").then((r) => json<Status>(r)),
  settings: () => fetch("/api/settings").then((r) => json<{ controls: Control[] }>(r)),
  patchSettings: (changes: Record<string, number>) =>
    fetch("/api/settings", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(changes),
    }).then((r) => json<{ controls: Control[] }>(r)),
  zoom: (r: { x0: number; y0: number; x1: number; y1: number }) =>
    fetch("/api/zoom", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(r) }),
  groundMask: () => fetch("/api/ground-mask").then((r) => json<{ on: boolean; alt: number[] | null }>(r)),
  setGroundMask: (body: { on?: boolean; alt?: number[] }) =>
    fetch("/api/ground-mask", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => json<{ on: boolean }>(r)),
  stretch: () => fetch("/api/stretch").then((r) => json<Stretch>(r)),
  setStretch: (s: Stretch) =>
    fetch("/api/stretch", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(s) })
      .then((r) => json<Stretch>(r)),
  preset: (name: "night" | "day") =>
    fetch(`/api/preset/${name}`, { method: "POST" })
      .then((r) => json<{ preset: string; stretch: Stretch; stack?: number }>(r)),
  stack: () => fetch("/api/stack").then((r) => json<{ frames: number }>(r)),
  setStack: (frames: number) =>
    fetch("/api/stack", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ frames }) })
      .then((r) => json<{ frames: number }>(r)),
  sky: (maxMag = 5.5) => fetch(`/api/sky?max_mag=${maxMag}`).then((r) => json<Sky>(r)),
  calibration: () => fetch("/api/calibration").then((r) => json<Calibration>(r)),
  calibrate: () => fetch("/api/calibration", { method: "POST" }).then((r) => json<Calibration>(r)),
  setCalibrationAuto: (auto: boolean) =>
    fetch("/api/calibration", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ auto }) })
      .then((r) => json<Calibration>(r)),
  setMode: (mode: Mode) =>
    fetch("/api/mode", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    }).then((r) => json<{ mode: Mode; smooth_until: string | null; stream_path: string }>(r)),
};
