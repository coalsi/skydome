import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Body, type Moving, type PlaneRow, type SatRow, type StarRow } from "./api";
import { useMedia } from "./controls";
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
const ZOOM_STEP = 1.8;     // the + and - buttons
const DOUBLE_TAP_ZOOM = 3;
const DOUBLE_TAP_MS = 300;
const CONTROLS_HIDE_MS = 4000; // full view: out of the picture's way until it's touched
const PHONE_EVERY_MS = 2000;   // keeps the phone stream running
const NO_PICTURE_MS = 6000;    // connected this long with nothing drawn: this device can't show it
const SKY_EVERY_MS = 20000;   // stars drift ~0.08° in that
const MOVING_EVERY_MS = 2000; // planes and satellites (D-030)
const COMPASS: [string, number, boolean][] = [
  ["N", 0, true], ["NE", 45, false], ["E", 90, true], ["SE", 135, false],
  ["S", 180, true], ["SW", 225, false], ["W", 270, true], ["NW", 315, false],
];

// What's under the pointer, and where it is on screen
type Hover = { x: number; y: number } & (
  | { kind: "star"; star: StarRow }
  | { kind: "body"; body: Body }
  | { kind: "sat"; sat: SatRow }
  | { kind: "plane"; plane: PlaneRow }
);

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

// Planets, the Moon, satellites and aircraft (D-030), every couple of seconds while labels are on
function useMoving(on: boolean) {
  const [moving, setMoving] = useState<Moving | null>(null);
  useEffect(() => {
    if (!on) return;
    const load = () => api.moving().then(setMoving).catch(() => {});
    load();
    const t = window.setInterval(load, MOVING_EVERY_MS);
    return () => window.clearInterval(t);
  }, [on]);
  return on ? moving : null;
}

// Satellites worth drawing: in sunlight while the sky's dark enough to see them
const litSats = (m: Moving | null) => (m && m.sun_alt < -6 ? m.satellites.filter((s) => s[5] && s[3] > 0) : []);

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

// The live dome over WebRTC: pinch, double tap, wheel or +/- to zoom, drag to pan;
// full-detail zoom stream when zoomed in; the ground-mask editor when `mask` is set
export function DomeView({ path, zoomPath, phonePath, edgeDeg, north, aspect, mask, track, full, canExit, onFull, onMenu }: {
  path: string; zoomPath: string; edgeDeg: number; mask: MaskEdit | null;
  phonePath: string | null; // the whole picture, for a screen that can't decode the main stream
  north: number;  // where north sits, degrees counterclockwise from the top
  aspect: number; // the stream's width / height: it's the middle of the square dome
  track?: [number, number, number][] | null; // an object from the sky watcher's log: [t, az, alt]
  full: boolean;      // over the whole screen
  canExit: boolean;   // asked for, rather than the phone being on its side
  onFull: () => void;
  onMenu: () => void; // the panels, in the full view
}) {
  const frame = useRef<HTMLDivElement>(null);
  const video = useRef<HTMLVideoElement>(null);
  const zoomVideo = useRef<HTMLVideoElement>(null);
  const [view, setView] = useState<View>(HOME);
  const [size, setSize] = useState({ W: 0, H: 0 });
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const pinch = useRef<{ d: number; mx: number; my: number; v: View } | null>(null);
  // A phone can't decode the 5120 wide main stream, and its screen couldn't show it
  const touch = useMedia("(pointer: coarse)");
  const phone = touch && !!phonePath;
  const conn = useWhep(phone ? phonePath! : path, video);
  const [noPicture, setNoPicture] = useState(false);
  const stars = useStars();
  const [labels, setLabels] = useState(readLabels);
  const moving = useMoving(labels && !mask);
  const [hover, setHover] = useState<Hover | null>(null);
  const tap = useRef<{ x: number; y: number; t: number } | null>(null);
  const lastTap = useRef<{ x: number; y: number; t: number } | null>(null);
  const lastPointer = useRef("mouse");
  const [glide, setGlide] = useState(false);   // buttons and double taps ease; fingers track exactly
  const [saving, setSaving] = useState(false);
  const [controls, setControls] = useState(true);
  const hideControls = useRef<number>();
  const upright = useMedia("(orientation: portrait) and (pointer: coarse)");

  useEffect(() => {
    if (!phone) return;
    api.phone().catch(() => {});
    const t = window.setInterval(() => api.phone().catch(() => {}), PHONE_EVERY_MS);
    return () => window.clearInterval(t);
  }, [phone]);

  // Connected and sending, and the video still has no size: say so, not a silent black
  useEffect(() => {
    setNoPicture(false);
    if (conn !== "live") return;
    const t = window.setTimeout(() => setNoPicture(!video.current?.videoWidth), NO_PICTURE_MS);
    return () => window.clearTimeout(t);
  }, [conn]);

  // Track the area's size (layout, rotation, full screen)
  useEffect(() => {
    const el = frame.current!;
    const ro = new ResizeObserver(() => setSize({ W: el.clientWidth, H: el.clientHeight }));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  // Across the dome's square: the stream's width, so a 16:9 stream's square overflows it above and below
  const D = Math.min(size.W, size.H * aspect);

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
  // The video fills the whole area, centred in it (contain).
  // Zoomed in, the dome may cover the area: then it can't be dragged off an
  // edge. While it's still smaller than the area along an axis, it stays centred
  const clamp = useCallback((v: View): View => {
    const W = frame.current?.clientWidth ?? 0;
    const H = frame.current?.clientHeight ?? 0;
    const D = Math.min(W, H * aspect);
    const s = Math.min(MAX_ZOOM, Math.max(1, v.s));
    const axis = (pos: number, side: number, extent: number) => {
      const inset = (s * (side - extent)) / 2; // layer edge to picture edge, scaled
      if (s * extent <= side) return (side - s * extent) / 2 - inset;
      return Math.min(-inset, Math.max(side - inset - s * extent, pos));
    };
    return { s, x: axis(v.x, W, D), y: axis(v.y, H, D / aspect) };
  }, [aspect]);

  // Rotating or going full screen changes the room: keep the view legal in the new one
  useEffect(() => setView((v) => clamp(v)), [size, clamp]);

  const zoomAt = (v: View, factor: number, cx: number, cy: number): View => {
    const s = Math.min(MAX_ZOOM, Math.max(1, v.s * factor));
    const k = s / v.s;
    return clamp({ s, x: cx - (cx - v.x) * k, y: cy - (cy - v.y) * k });
  };

  const zoomBy = (factor: number) => {
    setGlide(true);
    setView((v) => (factor < 1 && v.s / ZOOM_STEP < 1.1 ? HOME : zoomAt(v, factor, size.W / 2, size.H / 2)));
  };

  // Full view: the controls fade until the picture's touched. Any touch also
  // starts the video where autoplay is held back (an iPhone in Low Power Mode)
  const wake = useCallback(() => {
    setControls(true);
    window.clearTimeout(hideControls.current);
    if (full) hideControls.current = window.setTimeout(() => setControls(false), CONTROLS_HIDE_MS);
    for (const v of [video.current, zoomVideo.current]) if (v?.paused && v.srcObject) v.play().catch(() => {});
  }, [full]);
  useEffect(() => {
    wake();
    return () => window.clearTimeout(hideControls.current);
  }, [wake]);
  const shown = controls || saving || !full;

  // Safari's own pinch: two fingers on the dome are for the dome. The rest of the page still zooms
  useEffect(() => {
    const el = frame.current!;
    const stop = (e: Event) => e.preventDefault();
    el.addEventListener("gesturestart", stop, { passive: false });
    el.addEventListener("gesturechange", stop, { passive: false });
    return () => {
      el.removeEventListener("gesturestart", stop);
      el.removeEventListener("gesturechange", stop);
    };
  }, []);

  // Wheel needs a non-passive listener to stop the page scrolling
  useEffect(() => {
    const el = frame.current!;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const r = el.getBoundingClientRect();
      setGlide(false);
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
  // (azimuth, altitude) -> screen: the stitch's layout, east to north's left, the edge at edgeDeg
  const toScreen = (az: number, alt: number) => {
    const rho = (90 - alt) / edgeDeg / 2;
    const a = ((az + north) * Math.PI) / 180;
    const u = 0.5 - rho * Math.sin(a), v = 0.5 - rho * Math.cos(a);
    return { x: view.x + view.s * ((size.W - D) / 2 + u * D), y: view.y + view.s * ((size.H - D) / 2 + v * D) };
  };
  // The picture on screen: a wide stream shows the middle of the dome, and what's
  // in the sky above and below it isn't in the picture
  const shownRect = {
    x: view.x + view.s * ((size.W - D) / 2),
    y: view.y + view.s * ((size.H - D / aspect) / 2),
    w: view.s * D,
    h: (view.s * D) / aspect,
  };
  const inPicture = (q: { x: number; y: number }) =>
    q.x >= shownRect.x && q.x <= shownRect.x + shownRect.w && q.y >= shownRect.y && q.y <= shownRect.y + shownRect.h;
  // What's nearest the point within reach: aircraft, satellites, planets and the Moon
  // first, then stars, leaning towards the brighter ones
  const starAt = (p: { x: number; y: number }, reach: number): Hover | null => {
    let best: Hover | null = null, score = Infinity;
    const consider = (az: number, alt: number, bias: number, make: (q: { x: number; y: number }) => Hover) => {
      const q = toScreen(az, alt);
      const d = Math.hypot(q.x - p.x, q.y - p.y);
      if (inPicture(q) && d <= reach && d + bias < score) [best, score] = [make(q), d + bias];
    };
    for (const pl of moving?.planes ?? []) consider(pl[2], pl[3], -6, (q) => ({ kind: "plane", plane: pl, ...q }));
    for (const sat of litSats(moving)) consider(sat[2], sat[3], -4, (q) => ({ kind: "sat", sat, ...q }));
    for (const b of moving?.bodies ?? []) {
      if (b.kind !== "sun" && b.alt > 0) consider(b.az, b.alt, -5, (q) => ({ kind: "body", body: b, ...q }));
    }
    for (const st of stars) consider(st[4], st[5], st[3] * 1.5, (q) => ({ kind: "star", star: st, ...q }));
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
  // A point on screen -> (azimuth, altitude) on the dome
  const skyAt = (p: { x: number; y: number }) => {
    const u = ((p.x - view.x) / view.s - (size.W - D) / 2) / D;
    const v = ((p.y - view.y) / view.s - (size.H - D) / 2) / D;
    const dx = 2 * u - 1, dy = 2 * v - 1;
    const az = ((Math.atan2(-dx, -dy) * 180) / Math.PI - north + 720) % 360;
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
    lastPointer.current = e.pointerType;
    setGlide(false);
    setSaving(false);
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
      if (e.pointerType === "mouse") {
        if (full) wake();
        if (labels && !mask) setHover(starAt(local(e), 14));
      }
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
    // A tap (touch) names the star under it, or clears the name; two zoom in, or back out
    if (tap.current && e.pointerType !== "mouse" && !mask && performance.now() - tap.current.t < 500) {
      const p = local(e), now = performance.now(), before = lastTap.current;
      if (before && now - before.t < DOUBLE_TAP_MS && Math.hypot(p.x - before.x, p.y - before.y) < 40) {
        lastTap.current = null;
        setHover(null);
        setGlide(true);
        setView((v) => (v.s > 1 ? HOME : zoomAt(v, DOUBLE_TAP_ZOOM, p.x, p.y)));
      } else {
        lastTap.current = { ...p, t: now };
        if (labels) setHover(starAt(p, 28));
      }
    }
    tap.current = null;
    pointers.current.delete(e.pointerId);
    if (pointers.current.size < 2) pinch.current = null;
  };

  const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");

  return (
    <div className="relative h-full w-full bg-black" onPointerDownCapture={wake}>
      <div
        ref={frame}
        className="absolute inset-0 touch-none overflow-hidden select-none"
        style={{ cursor: mask ? "crosshair" : view.s > 1 ? "grab" : "zoom-in" }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onPointerLeave={(e) => e.pointerType === "mouse" && pointers.current.size === 0 && setHover(null)}
        onDoubleClick={(e) => {
          if (mask || lastPointer.current !== "mouse") return;
          const p = local(e as unknown as React.PointerEvent);
          setGlide(true);
          setView((v) => (v.s > 1 ? HOME : zoomAt(v, DOUBLE_TAP_ZOOM, p.x, p.y)));
        }}
      >
        <div
          className={`absolute inset-0 origin-top-left ${glide ? "transition-transform duration-200 ease-out motion-reduce:transition-none" : ""}`}
          style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.s})` }}
        >
          <video ref={video} className="h-full w-full object-contain" style={{ filter: "var(--video-filter)" }}
            autoPlay muted playsInline />
          {mask && D > 0 && (
            <MaskOverlay alt={mask.alt} edgeDeg={edgeDeg} north={north}
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
            <clipPath id="dome-shown">
              <rect x={shownRect.x} y={shownRect.y} width={shownRect.w} height={shownRect.h} />
            </clipPath>
            <g clipPath="url(#dome-shown)">
            {moving?.bodies.filter((b) => b.kind !== "sun" && b.alt > 0).map((b) => {
              const q = toScreen(b.az, b.alt);
              return (
                <g key={b.name}>
                  <circle cx={q.x} cy={q.y} r={b.kind === "moon" ? 9 : 4} fill="none" stroke="#fb923c" strokeWidth={1.2} />
                  <text x={q.x + (b.kind === "moon" ? 13 : 8)} y={q.y} dominantBaseline="central" fontSize={11}
                    fill="#fdba74" stroke="rgba(0,0,0,0.85)" strokeWidth={3} paintOrder="stroke">{b.name}</text>
                </g>
              );
            })}
            {litSats(moving).map((sat) => {
              const q = toScreen(sat[2], sat[3]);
              const named = !sat[0].startsWith("STARLINK") && !sat[0].startsWith("NORAD");
              return (
                <g key={sat[1]}>
                  <rect x={q.x - 2.5} y={q.y - 2.5} width={5} height={5} transform={`rotate(45 ${q.x} ${q.y})`}
                    fill="#22d3ee" opacity={0.85} />
                  {named && (
                    <text x={q.x + 6} y={q.y - 6} fontSize={10} fill="#67e8f9" stroke="rgba(0,0,0,0.85)" strokeWidth={3}
                      paintOrder="stroke">{sat[0]}</text>
                  )}
                </g>
              );
            })}
            {moving?.planes?.map((pl) => {
              const q = toScreen(pl[2], pl[3]);
              // Heading on the dome: east is on the left, so a plane's track turns the other way
              const rot = -pl[7];
              return (
                <g key={pl[1]}>
                  <path d="M0,-6 L4,5 L0,3 L-4,5 Z" transform={`translate(${q.x} ${q.y}) rotate(${rot})`}
                    fill="#fde047" stroke="rgba(0,0,0,0.8)" strokeWidth={1} />
                  <text x={q.x + 8} y={q.y + 3} fontSize={10} fill="#fef08a" stroke="rgba(0,0,0,0.85)" strokeWidth={3}
                    paintOrder="stroke">{pl[0]}</text>
                </g>
              );
            })}
            </g>
            {hover && <circle cx={hover.x} cy={hover.y} r={10} fill="none" stroke="#facc15" strokeWidth={1.5} />}
          </svg>
        )}
        {track && track.length > 0 && D > 0 && (
          <svg className="pointer-events-none absolute inset-0 h-full w-full" aria-hidden="true">
            {(() => {
              const pts = track.map(([, az, alt]) => toScreen(az, alt));
              const a = pts[0], b = pts[pts.length - 1];
              return (
                <>
                  <polyline points={pts.map((p) => `${p.x},${p.y}`).join(" ")} fill="none" stroke="#f472b6"
                    strokeWidth={2.5} strokeLinecap="round" strokeLinejoin="round" opacity={0.9} />
                  <circle cx={a.x} cy={a.y} r={5} fill="#f472b6" />
                  <circle cx={b.x} cy={b.y} r={9} fill="none" stroke="#f472b6" strokeWidth={2} />
                </>
              );
            })()}
          </svg>
        )}
        {labels && !mask && hover && (
          <StarTip hover={hover} W={size.W} H={size.H} />
        )}
        {(conn !== "live" || noPicture) && (
          <div className="absolute inset-0 flex items-center justify-center p-4">
            <div className="rounded-2xl bg-panel/90 px-4 py-2 text-center text-sm text-muted ring-1 ring-line">
              {noPicture ? "Connected, but this device isn't showing the video. Try reloading the page."
                : conn === "waiting" ? "Waiting for the live dome to start…" : "Connecting to the dome…"}
            </div>
          </div>
        )}
      </div>

      <div className={`overlay-sides pointer-events-none absolute inset-x-0 top-0 flex items-start justify-between gap-2 pt-2 transition-opacity duration-300 ${shown ? "" : "opacity-0"}`}>
        <div className="flex flex-col items-start gap-1.5">
          {zoomWanted && (
            <div className="rounded-full bg-panel/85 px-3 py-1 text-xs text-muted ring-1 ring-line backdrop-blur">
              {sharp ? "Full detail" : zoomConn === "live" ? "Sharpening…" : "Starting full detail…"}
              <span className="tabular ml-1.5 text-fg">{view.s.toFixed(1)}×</span>
            </div>
          )}
          {full && upright && !mask && (
            <div className="rounded-full bg-panel/85 px-3 py-1 text-xs text-muted ring-1 ring-line backdrop-blur">
              Turn sideways to fill the screen
            </div>
          )}
        </div>
        {full && (
          <div className={`flex gap-2 ${shown ? "pointer-events-auto" : ""}`}>
            <IconButton label="Settings" onClick={onMenu}>
              <path d="M4 7h16M4 12h16M4 17h16" /><circle cx="9" cy="7" r="2" /><circle cx="15" cy="12" r="2" /><circle cx="8" cy="17" r="2" />
            </IconButton>
            {canExit && (
              <IconButton label="Leave full screen" onClick={onFull}>
                <path d="M9 4v5H4M15 4v5h5M9 20v-5H4M15 20v-5h5" />
              </IconButton>
            )}
          </div>
        )}
      </div>

      <div className={`overlay-sides pointer-events-none absolute inset-x-0 bottom-0 flex items-end justify-between gap-2 pb-2 transition-opacity duration-300 ${shown ? "" : "opacity-0"}`}>
        <div className={`flex gap-2 ${shown ? "pointer-events-auto" : ""}`}>
          <IconButton label="Zoom out" onClick={() => zoomBy(1 / ZOOM_STEP)} disabled={view.s <= 1}>
            <path d="M6 12h12" />
          </IconButton>
          <IconButton label="Zoom in" onClick={() => zoomBy(ZOOM_STEP)} disabled={view.s >= MAX_ZOOM}>
            <path d="M12 6v12M6 12h12" />
          </IconButton>
        </div>
        <div className={`relative flex gap-2 ${shown ? "pointer-events-auto" : ""}`}>
          {saving && (
            <div className="absolute right-0 bottom-13 z-20 w-64 overflow-hidden rounded-xl bg-panel/95 ring-1 ring-line backdrop-blur">
              <SaveLink href={`/api/snapshot.jpg?t=${Date.now()}`} name={`skydome-${stamp}.jpg`}
                title="Snapshot" sub="As it streams" onDone={() => setSaving(false)} />
              <SaveLink href={`/api/full.jpg?t=${Date.now()}`} name={`skydome-full-${stamp}.jpg`}
                title="Full-detail still" sub="About 10 MB, takes a few seconds" onDone={() => setSaving(false)} />
              <SaveLink href={`/api/raw.zip?t=${Date.now()}`} title="Raw camera frame"
                sub="Exactly as the camera sent it, zipped" onDone={() => setSaving(false)} />
            </div>
          )}
          <IconButton label={labels ? "Hide compass, names, satellites and aircraft"
            : "Show compass, names, satellites and aircraft"} onClick={toggleLabels} active={labels}>
            <circle cx="12" cy="12" r="8.5" />
            <path d="M12 5.5l2 6.5-2 6.5-2-6.5z" />
          </IconButton>
          <IconButton label="Save a picture" onClick={() => setSaving(!saving)} active={saving}>
            <path d="M4 8h3l2-3h6l2 3h3v11H4z" />
            <circle cx="12" cy="13" r="3.5" />
          </IconButton>
          {!full && (
            <IconButton label="Full screen" onClick={onFull}>
              <path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5" />
            </IconButton>
          )}
        </div>
      </div>
    </div>
  );
}

function SaveLink({ href, name, title, sub, onDone }: {
  href: string; name?: string; title: string; sub: string; onDone: () => void;
}) {
  return (
    <a href={href} download={name ?? true} onClick={onDone}
      className="press flex min-h-11 flex-col justify-center border-b border-line px-3 py-1 last:border-b-0">
      <span className="text-sm text-fg">{title}</span>
      <span className="text-[11px] text-muted">{sub}</span>
    </a>
  );
}

// The masked ground, shaded, over the dome square (same layout as the stitch:
// north up, east left, the edge at edgeDeg from the zenith)
function MaskOverlay({ alt, edgeDeg, north, style }: { alt: number[]; edgeDeg: number; north: number; style: React.CSSProperties }) {
  const pts = alt.map((a, az) => {
    const rho = Math.min(1, Math.max(0, (90 - a) / edgeDeg)) / 2;
    const t = ((az + 0.5 + north) * Math.PI) / 180;
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

// What's under the pointer, by it, kept inside the view
function StarTip({ hover, W, H }: { hover: Hover; W: number; H: number }) {
  const left = hover.x + 230 > W ? hover.x - 230 : hover.x + 16;
  const top = Math.min(Math.max(hover.y - 12, 8), H - 84);
  const where = (az: number, alt: number) => `${Math.round(alt)}° up, ${compassPoint(az)} (${Math.round(az)}°)`;
  let title: string, line1: string, line2: string;
  if (hover.kind === "star") {
    const [name, desig, con, mag, az, alt] = hover.star;
    title = name;
    line1 = `${name !== desig ? `${desig} · ` : ""}${con || "—"}`;
    line2 = `mag ${mag.toFixed(1)} · ${where(az, alt)}`;
  } else if (hover.kind === "body") {
    const b = hover.body;
    title = b.name;
    line1 = b.kind === "moon" ? `Moon, ${Math.round((b.phase ?? 0) * 100)}% lit` : "Planet";
    line2 = where(b.az, b.alt);
  } else if (hover.kind === "sat") {
    const [name, norad, az, alt, km] = hover.sat;
    title = name;
    line1 = `Satellite · NORAD ${norad} · ${km.toLocaleString()} km away`;
    line2 = where(az, alt);
  } else {
    const [call, hex, az, alt, km, ft, kt] = hover.plane;
    title = call;
    line1 = `Aircraft ${hex.toUpperCase()} · ${ft.toLocaleString()} ft · ${kt} kt`;
    line2 = `${km.toFixed(km < 10 ? 1 : 0)} km away · ${where(az, alt)}`;
  }
  return (
    <div className="pointer-events-none absolute w-[214px] rounded-lg bg-panel/90 px-3 py-2 text-xs ring-1 ring-line backdrop-blur"
      style={{ left: Math.max(8, left), top }}>
      <div className="text-sm font-semibold text-fg">{title}</div>
      <div className="text-muted">{line1}</div>
      <div className="tabular text-muted">{line2}</div>
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

function IconButton({ label, onClick, active, disabled, children }: {
  label: string; onClick: () => void; active?: boolean; disabled?: boolean; children: React.ReactNode;
}) {
  return (
    <button type="button" onClick={onClick} title={label} aria-label={label} aria-pressed={active} disabled={disabled}
      className={`press flex h-11 w-11 items-center justify-center rounded-full bg-panel/85 ring-1 ring-line backdrop-blur hover:text-accent disabled:opacity-40 ${active ? "text-accent" : "text-fg"}`}>
      <Svg>{children}</Svg>
    </button>
  );
}
