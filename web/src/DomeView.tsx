import { useCallback, useEffect, useRef, useState } from "react";
import { playWhep, type WhepSession } from "./whep";

type Conn = "connecting" | "live" | "waiting";

interface View {
  s: number;
  x: number;
  y: number;
}

const MAX_ZOOM = 8;
const HOME: View = { s: 1, x: 0, y: 0 };

// The live dome over WebRTC, with wheel/pinch zoom and drag to pan
export function DomeView({ path }: { path: string }) {
  const frame = useRef<HTMLDivElement>(null);
  const video = useRef<HTMLVideoElement>(null);
  const [conn, setConn] = useState<Conn>("connecting");
  const [view, setView] = useState<View>(HOME);
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const pinch = useRef<{ d: number; mx: number; my: number; v: View } | null>(null);

  // Connect, and reconnect whenever the stream drops (mode switch, restart)
  useEffect(() => {
    let session: WhepSession | null = null;
    let timer: number | undefined;
    let stopped = false;
    const url = `/api/webrtc/${path}/whep`; // the server passes it on to mediamtx
    const retry = (ms: number) => {
      session?.close();
      session = null;
      if (!stopped) timer = window.setTimeout(connect, ms);
    };
    const connect = async () => {
      setConn((c) => (c === "live" ? "connecting" : c));
      try {
        session = await playWhep(url, video.current!, (state) => {
          if (state === "connected") setConn("live");
          if (state === "failed" || state === "disconnected" || state === "closed") {
            setConn("connecting");
            retry(1500);
          }
        });
        if (stopped) session.close();
      } catch {
        setConn("waiting");
        retry(3000);
      }
    };
    setConn("connecting");
    connect();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      session?.close();
    };
  }, [path]);

  const clamp = useCallback((v: View): View => {
    const w = frame.current?.clientWidth ?? 0;
    const s = Math.min(MAX_ZOOM, Math.max(1, v.s));
    const lo = w - w * s;
    return { s, x: Math.min(0, Math.max(lo, v.x)), y: Math.min(0, Math.max(lo, v.y)) };
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

  const onPointerDown = (e: React.PointerEvent) => {
    frame.current!.setPointerCapture(e.pointerId);
    pointers.current.set(e.pointerId, local(e));
    if (pointers.current.size === 2) {
      const [a, b] = [...pointers.current.values()];
      pinch.current = { d: Math.hypot(a.x - b.x, a.y - b.y), mx: (a.x + b.x) / 2, my: (a.y + b.y) / 2, v: view };
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
      setView((v) => clamp({ s: v.s, x: v.x + p.x - prev.x, y: v.y + p.y - prev.y }));
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
    // A size container, so the square dome can be as big as fits either way round
    <div className="relative flex h-full w-full items-center justify-center bg-black" style={{ containerType: "size" }}>
      <div
        ref={frame}
        className="relative aspect-square touch-none overflow-hidden select-none"
        style={{ width: "min(100cqw, 100cqh)", cursor: view.s > 1 ? "grab" : "zoom-in" }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onDoubleClick={() => setView(HOME)}
      >
        <div
          className="absolute inset-0 origin-top-left"
          style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.s})` }}
        >
          <video
            ref={video}
            className="h-full w-full object-contain"
            style={{ filter: "var(--video-filter)" }}
            autoPlay
            muted
            playsInline
          />
        </div>
        {conn !== "live" && (
          <div className="absolute inset-0 flex items-center justify-center">
            <div className="rounded-full bg-panel/90 px-4 py-2 text-sm text-muted ring-1 ring-line">
              {conn === "connecting" ? "Connecting to the dome…" : "Waiting for the live dome to start…"}
            </div>
          </div>
        )}
      </div>

      <div className="absolute right-3 bottom-3 flex gap-2">
        {view.s > 1 && (
          <IconButton label="Reset view" onClick={() => setView(HOME)}>
            <path d="M4 12a8 8 0 1 0 2.3-5.6M4 4v4h4" />
          </IconButton>
        )}
        <a
          href={`/api/snapshot.jpg?t=${Date.now()}`}
          download={`skydome-${stamp}.jpg`}
          title="Save a full-resolution snapshot"
          aria-label="Save a full-resolution snapshot"
          className="flex h-10 w-10 items-center justify-center rounded-full bg-panel/85 text-fg ring-1 ring-line backdrop-blur hover:text-accent"
        >
          <Svg>
            <path d="M4 8h3l2-3h6l2 3h3v11H4z" />
            <circle cx="12" cy="13" r="3.5" />
          </Svg>
        </a>
        <IconButton label="Full screen" onClick={fullscreen}>
          <path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5" />
        </IconButton>
      </div>
    </div>
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
    <button
      type="button"
      onClick={onClick}
      title={label}
      aria-label={label}
      className="flex h-10 w-10 items-center justify-center rounded-full bg-panel/85 text-fg ring-1 ring-line backdrop-blur hover:text-accent"
    >
      <Svg>{children}</Svg>
    </button>
  );
}
