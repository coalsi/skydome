import { useCallback, useEffect, useRef, useState } from "react";
import { api, type StarRow } from "./api";
import { useWhep } from "./useWhep";

interface View {
  s: number;
  x: number;
  y: number;
}

const MAX_ZOOM = 8;
const HOME: View = { s: 1, x: 0, y: 0 };
// Zoomed in at least this far, the Jetson renders the part in view straight
// from the cameras (D-023) and it fades in once the view holds still
const ZOOM_FROM = 1.4;
const ZOOM_ASPECT = 4 / 3; // the zoom stream is 1600 x 1200
const SETTLE_MS = 1200;    // request -> new picture on screen, with margin
const ZOOM_LINGER_MS = 20000;
const SKY_EVERY_MS = 20000;   // stars drift ~0.08° in that
const COMPASS: [string, number, boolean][] = [
  ["N", 0, true], ["NE", 45, false], ["E", 90, true], ["SE", 135, false],
  ["S", 180, true], ["SW", 225, false], ["W", 270, true], ["NW", 315, false],
];

interface Hover {
  star: StarRow;
  x: number;   // where the star is on screen
  y: number;
}

// The named stars up now (D-028), refreshed as they drift
function useStars() {
  const [stars, setStars] = useState<StarRow[]>([]);
  useEffect(() => {
    const load = () => api.sky().then((s) => setStars(s.stars)).catch(() => {});
    load();
    const t = window.setInterval(load, SKY_EVERY_MS);
    return () => window.clearInterval(t);
  }, []);
  return stars;
}

function readLabels(): boolean {
  try {
    return localStorage.getItem("skydome.labels") !== "off";
  } catch {
    return true;
  }
}

export interface MaskEdit {
  alt: number[];                     // 360 horizon altitudes, north then clockwise
  onChange: (alt: number[]) => void;
}

// The live dome over WebRTC: wheel/pinch zoom, drag to pan; full-detail zoom
// stream when zoomed in; the ground-mask editor when `mask` is set
export function DomeView({ path, zoomPath, edgeDeg, mask }: {
  path: string; zoomPath: string; edgeDeg: number; mask: MaskEdit | null;
}) {
  const frame = useRef<HTMLDivElement>(null);
  const video = useRef<HTMLVideoElement>(null);
  const zoomVideo = useRef<HTMLVideoElement>(null);
  const [view, setView] = useState<View>(HOME);
  const [size, setSize] = useState({ W: 0, H: 0 });
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const pinch = useRef<{ d: number; mx: number; my: number; v: View } | null>(null);
  const conn = useWhep(path, video);
  const stars = useStars();
  const [labels, setLabels] = useState(readLabels);
  const [hover, setHover] = useState<Hover | null>(null);
  const tap = useRef<{ x: number; y: number; t: number } | null>(null);

  // Track the area's size (layout, rotation, full screen)
  useEffect(() => {
    const el = frame.current!;
    const ro = new ResizeObserver(() => setSize({ W: el.clientWidth, H: el.clientHeight }));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const D = Math.min(size.W, size.H);

  // ---- zoom stream -------------------------------------------------------------
  const zoomWanted = view.s >= ZOOM_FROM && !mask;
  const [zoomOn, setZoomOn] = useState(false); // stays connected a while after zooming out
  const [settled, setSettled] = useState(false);
  const zoomConn = useWhep(zoomPath, zoomVideo, zoomOn);

  useEffect(() => {
    if (zoomWanted) {
      setZoomOn(true);
      return;
    }
    const t = window.setTimeout(() => setZoomOn(false), ZOOM_LINGER_MS);
    return () => window.clearTimeout(t);
  }, [zoomWanted]);

  // The part of the dome in view, in dome units (0..1 across its square),
  // widened to the zoom stream's 4:3 so object-fit: cover crops it back exactly
  const rect = useCallback(() => {
    const { W, H } = size;
    const toU = (px: number, v0: number, side: number) => ((px - v0) / view.s - (side - D) / 2) / D;
    let [u0, u1] = [toU(0, view.x, W), toU(W, view.x, W)];
    let [v0, v1] = [toU(0, view.y, H), toU(H, view.y, H)];
    const w = u1 - u0, h = v1 - v0;
    if (w / h < ZOOM_ASPECT) {
      const cu = (u0 + u1) / 2;
      [u0, u1] = [cu - (h * ZOOM_ASPECT) / 2, cu + (h * ZOOM_ASPECT) / 2];
    } else {
      const cv = (v0 + v1) / 2;
      [v0, v1] = [cv - w / ZOOM_ASPECT / 2, cv + w / ZOOM_ASPECT / 2];
    }
    return { x0: u0, y0: v0, x1: u1, y1: v1 };
  }, [size, view, D]);

  // Ask for the new rectangle shortly after the view changes, show the sharp
  // picture once it has had time to arrive, and keep asking while zoomed
  useEffect(() => {
    setSettled(false);
    if (!zoomWanted || !D) return;
    const send = () => api.zoom(rect()).catch(() => {});
    const t1 = window.setTimeout(send, 120);
    const t2 = window.setTimeout(() => setSettled(true), SETTLE_MS);
    const beat = window.setInterval(send, 1000);
    return () => {
      window.clearTimeout(t1);
      window.clearTimeout(t2);
      window.clearInterval(beat);
    };
  }, [zoomWanted, rect, D]);
  const sharp = zoomWanted && settled && zoomConn === "live";

  // ---- pan / zoom ----------------------------------------------------------------
  // The video fills the whole area with the square dome centred in it (contain).
  // Zoomed in, the dome may cover the area: then it can't be dragged off an
  // edge. While it's still smaller than the area along an axis, it stays centred
  const clamp = useCallback((v: View): View => {
    const W = frame.current?.clientWidth ?? 0;
    const H = frame.current?.clientHeight ?? 0;
    const D = Math.min(W, H);
    const s = Math.min(MAX_ZOOM, Math.max(1, v.s));
    const axis = (pos: number, side: number) => {
      const inset = (s * (side - D)) / 2; // layer edge to dome edge, scaled
      if (s * D <= side) return (side - s * D) / 2 - inset;
      return Math.min(-inset, Math.max(side - inset - s * D, pos));
    };
    return { s, x: axis(v.x, W), y: axis(v.y, H) };
  }, []);

  const zoomAt = (v: View, factor: number, cx: number, cy: number): View => {
    const s = Math.min(MAX_ZOOM, Math.max(1, v.s * factor));
    const k = s / v.s;
    return clamp({ s, x: cx - (cx - v.x) * k, y: cy - (cy - v.y) * k });
  };

  // Wheel needs a non-passive listener to stop the page scrolling
  useEffect(() => {
    const el = frame.current!;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const r = el.getBoundingClientRect();
      setView((v) => zoomAt(v, Math.exp(-e.deltaY * 0.0015), e.clientX - r.left, e.clientY - r.top));
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  });

  const local = (e: React.PointerEvent) => {
    const r = frame.current!.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };

  // ---- labels: compass, and the star under the pointer ------------------------------
  // (azimuth, altitude) -> screen: the stitch's layout, north up, east left, the edge at edgeDeg
  const toScreen = (az: number, alt: number) => {
    const rho = (90 - alt) / edgeDeg / 2;
    const a = (az * Math.PI) / 180;
    const u = 0.5 - rho * Math.sin(a), v = 0.5 - rho * Math.cos(a);
    return { x: view.x + view.s * ((size.W - D) / 2 + u * D), y: view.y + view.s * ((size.H - D) / 2 + v * D) };
  };
  // The star nearest the point within reach, leaning towards the brighter ones
  const starAt = (p: { x: number; y: number }, reach: number): Hover | null => {
    let best: Hover | null = null, score = Infinity;
    for (const st of stars) {
      const q = toScreen(st[4], st[5]);
      const d = Math.hypot(q.x - p.x, q.y - p.y);
      if (d > reach) continue;
      const sc = d + st[3] * 1.5;
      if (sc < score) [best, score] = [{ star: st, ...q }, sc];
    }
    return best;
  };
  const toggleLabels = () => {
    setLabels((on) => {
      try {
        localStorage.setItem("skydome.labels", on ? "off" : "on");
      } catch { /* private window: just this visit */ }
      return !on;
    });
    setHover(null);
  };

  // ---- ground-mask editor ------------------------------------------------------
  // A point on screen -> (azimuth, altitude) on the dome (north up, east left)
  const skyAt = (p: { x: number; y: number }) => {
    const u = ((p.x - view.x) / view.s - (size.W - D) / 2) / D;
    const v = ((p.y - view.y) / view.s - (size.H - D) / 2) / D;
    const dx = 2 * u - 1, dy = 2 * v - 1;
    const az = ((Math.atan2(-dx, -dy) * 180) / Math.PI + 360) % 360;
    return { az, alt: 90 - Math.hypot(dx, dy) * edgeDeg };
  };
  const paint = (p: { x: number; y: number }) => {
    if (!mask) return;
    const { az, alt } = skyAt(p);
    const next = mask.alt.slice();
    const a = Math.max(-10, Math.min(60, alt));
    for (let d = -2; d <= 2; d++) next[(Math.floor(az) + d + 360) % 360] = Math.round(a * 10) / 10;
    mask.onChange(next);
  };

  const onPointerDown = (e: React.PointerEvent) => {
    frame.current!.setPointerCapture(e.pointerId);
    const p = local(e);
    tap.current = pointers.current.size === 0 ? { ...p, t: performance.now() } : null;
    pointers.current.set(e.pointerId, p);
    if (pointers.current.size === 2) {
      const [a, b] = [...pointers.current.values()];
      pinch.current = { d: Math.hypot(a.x - b.x, a.y - b.y), mx: (a.x + b.x) / 2, my: (a.y + b.y) / 2, v: view };
    } else if (mask) {
      paint(p);
    }
  };

  const onPointerMove = (e: React.PointerEvent) => {
    const prev = pointers.current.get(e.pointerId);
    if (!prev) {
      // A mouse just passing over: name the star under it
      if (e.pointerType === "mouse" && labels && !mask) setHover(starAt(local(e), 14));
      return;
    }
    const p = local(e);
    if (tap.current && Math.hypot(p.x - tap.current.x, p.y - tap.current.y) > 6) tap.current = null;
    if (hover) setHover(null);
    pointers.current.set(e.pointerId, p);
    if (pointers.current.size === 2 && pinch.current) {
      const [a, b] = [...pointers.current.values()];
      const d = Math.hypot(a.x - b.x, a.y - b.y);
      const mx = (a.x + b.x) / 2;
      const my = (a.y + b.y) / 2;
      const base = pinch.current;
      const z = zoomAt(base.v, d / base.d, base.mx, base.my);
      setView(clamp({ s: z.s, x: z.x + mx - base.mx, y: z.y + my - base.my }));
    } else if (pointers.current.size === 1) {
      if (mask) paint(p);
      else setView((v) => clamp({ s: v.s, x: v.x + p.x - prev.x, y: v.y + p.y - prev.y }));
    }
  };

  const onPointerUp = (e: React.PointerEvent) => {
    // A tap (touch) names the star under it, or clears the name
    if (tap.current && e.pointerType !== "mouse" && labels && !mask && performance.now() - tap.current.t < 500) {
      setHover(starAt(local(e), 28));
    }
    tap.current = null;
    pointers.current.delete(e.pointerId);
    if (pointers.current.size < 2) pinch.current = null;
  };

  const fullscreen = () => {
    const el = frame.current?.parentElement;
    if (document.fullscreenElement) document.exitFullscreen();
    else el?.requestFullscreen?.();
  };

  const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");

  return (
    <div className="relative h-full w-full bg-black">
      <div
        ref={frame}
        className="absolute inset-0 touch-none overflow-hidden select-none"
        style={{ cursor: mask ? "crosshair" : view.s > 1 ? "grab" : "zoom-in" }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onPointerLeave={(e) => e.pointerType === "mouse" && pointers.current.size === 0 && setHover(null)}
        onDoubleClick={() => !mask && setView(HOME)}
      >
        <div
          className="absolute inset-0 origin-top-left"
          style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.s})` }}
        >
          <video ref={video} className="h-full w-full object-contain" style={{ filter: "var(--video-filter)" }}
            autoPlay muted playsInline />
          {mask && D > 0 && (
            <MaskOverlay alt={mask.alt} edgeDeg={edgeDeg}
              style={{ left: (size.W - D) / 2, top: (size.H - D) / 2, width: D, height: D }} />
          )}
        </div>
        {/* The part in view at the cameras' own sharpness; hidden at once when the view moves */}
        <video ref={zoomVideo} className="pointer-events-none absolute inset-0 h-full w-full object-cover"
          style={{ filter: "var(--video-filter)", opacity: sharp ? 1 : 0, transition: sharp ? "opacity 250ms" : "none" }}
          autoPlay muted playsInline />
        {labels && !mask && D > 0 && (
          <svg className="pointer-events-none absolute inset-0 h-full w-full" aria-hidden="true">
            {COMPASS.map(([t, az, main]) => {
              const q = toScreen(az, -4);
              return (
                <text key={t} x={q.x} y={q.y} textAnchor="middle" dominantBaseline="central"
                  fontSize={main ? 18 : 11} fontWeight={main ? 700 : 500}
                  fill={main ? (t === "N" ? "#f87171" : "#e5e7eb") : "#9ca3af"}
                  stroke="rgba(0,0,0,0.85)" strokeWidth={3} paintOrder="stroke">{t}</text>
              );
            })}
            {hover && <circle cx={hover.x} cy={hover.y} r={10} fill="none" stroke="#facc15" strokeWidth={1.5} />}
          </svg>
        )}
        {labels && !mask && hover && (
          <StarTip hover={hover} W={size.W} H={size.H} />
        )}
        {conn !== "live" && (
          <div className="absolute inset-0 flex items-center justify-center">
            <div className="rounded-full bg-panel/90 px-4 py-2 text-sm text-muted ring-1 ring-line">
              {conn === "waiting" ? "Waiting for the live dome to start…" : "Connecting to the dome…"}
            </div>
          </div>
        )}
      </div>

      {zoomWanted && (
        <div className="pointer-events-none absolute top-3 left-3 rounded-full bg-panel/85 px-3 py-1 text-xs text-muted ring-1 ring-line backdrop-blur">
          {sharp ? "Full detail" : zoomConn === "live" ? "Sharpening…" : "Starting full detail…"}
        </div>
      )}

      <div className="absolute right-3 bottom-3 flex gap-2">
        <IconButton label={labels ? "Hide compass and star names" : "Show compass and star names (hover or tap a star)"}
          onClick={toggleLabels} active={labels}>
          <circle cx="12" cy="12" r="8.5" />
          <path d="M12 5.5l2 6.5-2 6.5-2-6.5z" />
        </IconButton>
        {view.s > 1 && (
          <IconButton label="Reset view" onClick={() => setView(HOME)}>
            <path d="M4 12a8 8 0 1 0 2.3-5.6M4 4v4h4" />
          </IconButton>
        )}
        <a href={`/api/snapshot.jpg?t=${Date.now()}`} download={`skydome-${stamp}.jpg`}
          title="Save a snapshot of the dome as it streams" aria-label="Save a snapshot"
          className="flex h-10 w-10 items-center justify-center rounded-full bg-panel/85 text-fg ring-1 ring-line backdrop-blur hover:text-accent">
          <Svg>
            <path d="M4 8h3l2-3h6l2 3h3v11H4z" />
            <circle cx="12" cy="13" r="3.5" />
          </Svg>
        </a>
        <a href={`/api/full.jpg?t=${Date.now()}`} download={`skydome-full-${stamp}.jpg`}
          title="Save a full-detail still: the cameras' own sharpness, 6656 × 6656 (about 10 MB, takes a few seconds)"
          aria-label="Save a full-detail still"
          className="flex h-10 items-center gap-1.5 rounded-full bg-panel/85 px-3 text-xs font-medium text-fg ring-1 ring-line backdrop-blur hover:text-accent">
          <Svg>
            <path d="M4 8h3l2-3h6l2 3h3v11H4z" />
            <path d="M12 10v6M9 13h6" />
          </Svg>
          Full detail
        </a>
        <a href={`/api/raw.zip?t=${Date.now()}`} download
          title="Every pixel there is: each camera's frame exactly as it sent it, 5 x 3840 x 2160, zipped (no stitching)"
          aria-label="Download the raw camera frames"
          className="flex h-10 items-center gap-1.5 rounded-full bg-panel/85 px-3 text-xs font-medium text-fg ring-1 ring-line backdrop-blur hover:text-accent">
          <Svg>
            <rect x="3" y="5" width="8" height="6" rx="1" />
            <rect x="13" y="5" width="8" height="6" rx="1" />
            <rect x="8" y="13" width="8" height="6" rx="1" />
          </Svg>
          Raw
        </a>
        <IconButton label="Full screen" onClick={fullscreen}>
          <path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5" />
        </IconButton>
      </div>
    </div>
  );
}

// The masked ground, shaded, over the dome square (same layout as the stitch:
// north up, east left, the edge at edgeDeg from the zenith)
function MaskOverlay({ alt, edgeDeg, style }: { alt: number[]; edgeDeg: number; style: React.CSSProperties }) {
  const pts = alt.map((a, az) => {
    const rho = Math.min(1, Math.max(0, (90 - a) / edgeDeg)) / 2;
    const t = ((az + 0.5) * Math.PI) / 180;
    return `${(0.5 - rho * Math.sin(t)).toFixed(4)},${(0.5 - rho * Math.cos(t)).toFixed(4)}`;
  });
  const line = `M${pts.join("L")}Z`;
  const circle = "M0,0.5a0.5,0.5 0 1,0 1,0a0.5,0.5 0 1,0 -1,0Z";
  return (
    <svg className="pointer-events-none absolute" style={style} viewBox="0 0 1 1" aria-hidden="true">
      <path d={circle + line} fill="rgba(239,68,68,0.38)" fillRule="evenodd" />
      <path d={line} fill="none" stroke="#f87171" strokeWidth="0.003" />
    </svg>
  );
}

// A star's name by the pointer, kept inside the view
function StarTip({ hover, W, H }: { hover: Hover; W: number; H: number }) {
  const [name, desig, con, mag, az, alt] = hover.star;
  const left = hover.x + 230 > W ? hover.x - 230 : hover.x + 16;
  const top = Math.min(Math.max(hover.y - 12, 8), H - 84);
  return (
    <div className="pointer-events-none absolute w-[214px] rounded-lg bg-panel/90 px-3 py-2 text-xs ring-1 ring-line backdrop-blur"
      style={{ left: Math.max(8, left), top }}>
      <div className="text-sm font-semibold text-fg">{name}</div>
      <div className="text-muted">
        {name !== desig ? `${desig} · ` : ""}{con || "—"}
      </div>
      <div className="tabular text-muted">
        mag {mag.toFixed(1)} · {Math.round(alt)}° up, {compassPoint(az)} ({Math.round(az)}°)
      </div>
    </div>
  );
}

function compassPoint(az: number): string {
  const pts = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
  return pts[Math.round(az / 22.5) % 16];
}

function Svg({ children }: { children: React.ReactNode }) {
  return (
    <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.8"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {children}
    </svg>
  );
}

function IconButton({ label, onClick, active, children }: {
  label: string; onClick: () => void; active?: boolean; children: React.ReactNode;
}) {
  return (
    <button type="button" onClick={onClick} title={label} aria-label={label} aria-pressed={active}
      className={`flex h-10 w-10 items-center justify-center rounded-full bg-panel/85 ring-1 ring-line backdrop-blur hover:text-accent ${active ? "text-accent" : "text-fg"}`}>
      <Svg>{children}</Svg>
    </button>
  );
}
