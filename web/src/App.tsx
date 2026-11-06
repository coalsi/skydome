import { useEffect, useRef, useState } from "react";
import { api, type Mode, type SkyObject, type Status } from "./api";
import { PHONE_SIDEWAYS, useMedia } from "./controls";
import { DomeView } from "./DomeView";
import { GroundMask } from "./GroundMask";
import { SkyPanel } from "./SkyPanel";
import { HealthPanel } from "./HealthPanel";
import { ObjectsPanel } from "./ObjectsPanel";
import { SettingsPanel } from "./SettingsPanel";

type Tab = "sky" | "camera" | "objects" | "health";
const TABS: [Tab, string, React.ReactNode][] = [
  ["sky", "Sky", <><circle cx="12" cy="12" r="4" /><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6 7 7M17 17l1.4 1.4M5.6 18.4 7 17M17 7l1.4-1.4" /></>],
  ["camera", "Camera", <><path d="M4 7h16M4 12h16M4 17h16" /><circle cx="9" cy="7" r="2" /><circle cx="15" cy="12" r="2" /><circle cx="8" cy="17" r="2" /></>],
  ["objects", "Objects", <><path d="M4 18 18 6" /><circle cx="18.5" cy="5.5" r="2" /><path d="M5 9l1.5-1.5M12 19l1.5-1.5" /></>],
  ["health", "Health", <path d="M3 12h4l2.5-6 4 12 2.5-6h5" />],
];

function useStatus() {
  const [status, setStatus] = useState<Status | null>(null);
  const [online, setOnline] = useState(true);
  useEffect(() => {
    let stop = false;
    const poll = () =>
      api
        .status()
        .then((s) => !stop && (setStatus(s), setOnline(true)))
        .catch(() => !stop && setOnline(false));
    poll();
    const t = window.setInterval(poll, 1000);
    return () => {
      stop = true;
      window.clearInterval(t);
    };
  }, []);
  return { status, setStatus, online };
}

function useNightTheme(): [boolean, (on: boolean) => void] {
  const [night, setNight] = useState(() => {
    try {
      return localStorage.getItem("skydome-theme") === "night";
    } catch {
      return false;
    }
  });
  useEffect(() => {
    document.documentElement.dataset.theme = night ? "night" : "";
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", night ? "#0a0000" : "#12161e");
    try {
      localStorage.setItem("skydome-theme", night ? "night" : "day");
    } catch {
      /* private window: just don't remember */
    }
  }, [night]);
  return [night, setNight];
}

// The picture over the whole screen. The page does it itself, because an iPhone
// has no full-screen call for anything but its own video player, and that one
// can't pinch into the dome. Where the browser can, it hides its own bars too
function useFullView() {
  const sideways = useMedia(PHONE_SIDEWAYS);
  const [asked, setAsked] = useState(false);
  const browserFull = useRef(false);

  useEffect(() => {
    const changed = () => {
      if (browserFull.current && !document.fullscreenElement) {   // Esc, or Android's back
        browserFull.current = false;
        setAsked(false);
      }
    };
    document.addEventListener("fullscreenchange", changed);
    return () => document.removeEventListener("fullscreenchange", changed);
  }, []);

  const enter = async () => {
    setAsked(true);
    try {
      await document.documentElement.requestFullscreen?.();
      browserFull.current = !!document.fullscreenElement;
      await (screen.orientation as unknown as { lock?: (o: string) => Promise<void> })?.lock?.("landscape");
    } catch { /* the page's own full view is already up */ }
  };
  const leave = () => {
    setAsked(false);
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    try {
      screen.orientation?.unlock?.();
    } catch { /* nothing locked */ }
  };
  return { full: asked || sideways, asked, enter, leave };
}

export default function App() {
  const { status, setStatus, online } = useStatus();
  const [night, setNight] = useNightTheme();
  // An alert's link (#objects/<id>) opens the Objects tab
  const [tab, setTab] = useState<Tab>(() => (location.hash.startsWith("#objects") ? "objects" : "sky"));
  const [picked, setPicked] = useState<SkyObject | null>(null);
  const [switching, setSwitching] = useState(false);
  const [maskEdit, setMaskEdit] = useState<number[] | null>(null);
  const { full, asked, enter, leave } = useFullView();
  const [sheet, setSheet] = useState(false);   // the panels, over the full view

  const setMode = (mode: Mode) => {
    if (!status || status.mode === mode) return;
    setSwitching(true);
    api
      .setMode(mode)
      .then((r) => setStatus({ ...status, mode: r.mode, smooth_until: r.smooth_until,
                               stream: { ...status.stream, path: r.stream_path } }))
      .finally(() => setSwitching(false));
  };

  const live = status?.live;
  const path = status?.stream.path ?? "dome";
  const aspect = status?.stream_aspect ?? 16 / 9;

  return (
    // Phone: the picture stays put, the panel scrolls under it, the tabs are by the thumb.
    // Desktop: side by side. The same tree throughout, so the video never reconnects
    <div className="flex h-dvh flex-col overflow-hidden lg:flex-row">
      <main className={full ? "fixed inset-0 z-40 bg-black"
        : "relative flex shrink-0 flex-col lg:min-h-0 lg:flex-1 lg:shrink"}>
        {!full && (
          <header className="safe-top flex items-center justify-between gap-3 border-b border-line bg-panel px-3">
            <div className="flex h-11 min-w-0 items-center gap-2">
              <img src="/favicon.svg" alt="" className="h-6 w-6" />
              <h1 className="text-base font-semibold tracking-tight">SkyDome</h1>
              <LiveBadge online={online} running={!!live} fps={live?.output_fps} />
            </div>
            <button type="button" onClick={() => setNight(!night)} aria-pressed={night}
              title="Red night-vision colours, so your eyes stay dark-adapted"
              className={`press h-9 shrink-0 rounded-full px-3 text-xs ring-1 ring-line ${night ? "bg-accent text-bg" : "text-muted hover:text-fg"}`}>
              Red light
            </button>
          </header>
        )}
        <div className={full ? "h-full w-full" : "w-full lg:min-h-0 lg:flex-1"}
          style={full ? undefined : { aspectRatio: aspect }}>
          <DomeView path={path} zoomPath={status?.zoom_path ?? "dome-zoom"} edgeDeg={status?.edge_zenith_deg ?? 100}
            north={status?.north_deg ?? 0} aspect={aspect} phonePath={status?.phone_path ?? null}
            full={full} onFull={asked ? leave : enter} canExit={asked} onMenu={() => setSheet(true)}
            mask={maskEdit ? { alt: maskEdit, onChange: setMaskEdit } : null}
            track={tab === "objects" ? picked?.path ?? null : null} />
        </div>
      </main>

      {full && sheet && (
        <button type="button" aria-label="Close" onClick={() => setSheet(false)}
          className="fixed inset-0 z-[45] bg-black/50" />
      )}

      <aside className={full
        ? (sheet ? "safe-sides fixed inset-y-0 right-0 z-50 flex w-[min(24rem,88vw)] flex-col border-l border-line bg-panel" : "hidden")
        : "flex min-h-0 flex-1 flex-col border-line bg-panel lg:w-[380px] lg:flex-none lg:border-l"}>
        {full && (
          <div className="safe-top flex items-center justify-between border-b border-line pl-4">
            <span className="text-sm font-medium">Settings</span>
            <button type="button" aria-label="Close settings" onClick={() => setSheet(false)}
              className="press flex h-11 w-11 items-center justify-center text-muted">✕</button>
          </div>
        )}
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain p-4 lg:order-last">
          <div className={tab === "sky" ? "space-y-5" : "hidden"}>
            <ModeSwitch mode={status?.mode} until={status?.smooth_until ?? null} busy={switching} onChange={setMode} />
            <SkyPanel />
            <div className="border-t border-line pt-4">
              <GroundMask status={status} editing={maskEdit} onEdit={setMaskEdit} />
            </div>
          </div>
          {tab === "camera" && <SettingsPanel />}
          {tab === "objects" && <ObjectsPanel selected={picked?.id ?? null} onSelect={(o) => {
            setPicked(o);
            if (o && full) setSheet(false);   // the path is on the dome behind the sheet
          }} />}
          {tab === "health" && <HealthPanel status={status} />}
        </div>
        <nav className="safe-bottom flex shrink-0 border-t border-line bg-panel lg:order-first lg:border-t-0 lg:border-b"
          role="tablist" aria-label="Panels">
          {TABS.map(([t, name, icon]) => (
            <button key={t} type="button" role="tab" aria-selected={tab === t} onClick={() => setTab(t)}
              className={`press flex h-14 flex-1 flex-col items-center justify-center gap-0.5 text-[11px] font-medium lg:h-12 ${tab === t ? "text-accent" : "text-muted hover:text-fg"}`}>
              <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.8"
                strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{icon}</svg>
              {name}
            </button>
          ))}
        </nav>
      </aside>
    </div>
  );
}

function LiveBadge({ online, running, fps }: { online: boolean; running: boolean; fps?: number }) {
  const [dot, text] = !online
    ? ["bg-bad", "Offline"]
    : running
      ? ["bg-good", `Live · ${fps?.toFixed(0)} fps`]
      : ["bg-warn", "Stopped"];
  return (
    <span className="flex shrink-0 items-center gap-1.5 rounded-full bg-raised px-2.5 py-0.5 text-xs text-muted ring-1 ring-line">
      <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />
      <span className="tabular">{text}</span>
    </span>
  );
}

function ModeSwitch({ mode, until, busy, onChange }: {
  mode?: Mode; until: string | null; busy: boolean; onChange: (m: Mode) => void;
}) {
  const left = until ? Math.max(0, Math.ceil((Date.parse(until) - Date.now()) / 60000)) : null;
  const options: [Mode, string, string][] = [
    ["detail", "Detail", "5120 × 2880 · 15 fps"],
    ["smooth", "Smooth", "2048 × 1152 · 30 fps"],
  ];
  return (
    <div>
      <div className="grid grid-cols-2 gap-1 rounded-xl bg-raised p-1 ring-1 ring-line" role="radiogroup" aria-label="Dome mode">
        {options.map(([m, label, sub]) => (
          <button key={m} type="button" role="radio" aria-checked={mode === m} disabled={busy || !mode}
            onClick={() => onChange(m)}
            className={`press min-h-14 rounded-lg px-3 py-2 text-left transition-colors ${mode === m ? "bg-accent text-bg" : "text-muted hover:text-fg"}`}>
            <div className="text-sm font-medium">{label}</div>
            <div className={`tabular text-[11px] ${mode === m ? "opacity-75" : "opacity-60"}`}>{sub}</div>
          </button>
        ))}
      </div>
      <p className="mt-2 text-xs leading-relaxed text-muted">
        {mode === "smooth"
          ? `Back to Detail in ${left} min. The NVR stream pauses while Smooth is on.`
          : "Switching restarts the dome; the picture is back in a few seconds."}
      </p>
    </div>
  );
}
