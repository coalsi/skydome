// The server's API (server/skydome_server/app.py)

export type Mode = "detail" | "smooth";

export interface CameraStats {
  fps: number;
  state: string;
  restarts: number;
  damaged_dropped: number;
  // Brightness/colour matching to the neighbours (D-022): Y x gain, U/V shifted
  balance?: { gain: number; u: number; v: number };
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

async function json<T>(r: Response): Promise<T> {
  if (!r.ok) throw new Error((await r.text()) || `${r.status}`);
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
    fetch(`/api/preset/${name}`, { method: "POST" }).then((r) => json<{ preset: string; stretch: Stretch }>(r)),
  setMode: (mode: Mode) =>
    fetch("/api/mode", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    }).then((r) => json<{ mode: Mode; smooth_until: string | null; stream_path: string }>(r)),
};
