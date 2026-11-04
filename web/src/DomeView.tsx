import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
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
    if (!prev) return;
    const p = local(e);
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

function Svg({ children }: { children: React.ReactNode }) {
  return (
    <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.8"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {children}
    </svg>
  );
}

function IconButton({ label, onClick, children }: { label: string; onClick: () => void; children: React.ReactNode }) {
  return (
    <button type="button" onClick={onClick} title={label} aria-label={label}
      className="flex h-10 w-10 items-center justify-center rounded-full bg-panel/85 text-fg ring-1 ring-line backdrop-blur hover:text-accent">
      <Svg>{children}</Svg>
    </button>
  );
}
