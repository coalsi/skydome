import { useEffect, useState } from "react";
import { api, type Mode, type Status } from "./api";
import { DomeView } from "./DomeView";
import { GroundMask } from "./GroundMask";
import { HealthPanel } from "./HealthPanel";
import { SettingsPanel } from "./SettingsPanel";

type Tab = "cameras" | "health";

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
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", night ? "#0a0000" : "#0b0d12");
    try {
      localStorage.setItem("skydome-theme", night ? "night" : "day");
    } catch {
      /* private window: just don't remember */
    }
  }, [night]);
  return [night, setNight];
}

export default function App() {
  const { status, setStatus, online } = useStatus();
  const [night, setNight] = useNightTheme();
  const [tab, setTab] = useState<Tab>("cameras");
  const [switching, setSwitching] = useState(false);
  const [maskEdit, setMaskEdit] = useState<number[] | null>(null);

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

  return (
    // Phone: the dome, then the panel, and the page scrolls. Desktop: side by side, full height
    <div className="flex min-h-full flex-col lg:h-full lg:flex-row">
      <main className="relative flex shrink-0 flex-col lg:min-h-0 lg:flex-1 lg:shrink">
        <header className="flex items-center justify-between gap-3 border-b border-line bg-panel px-4 py-2.5">
          <div className="flex items-center gap-2.5">
            <img src="/favicon.svg" alt="" className="h-6 w-6" />
            <h1 className="text-base font-semibold tracking-tight">SkyDome</h1>
            <LiveBadge online={online} running={!!live} fps={live?.output_fps} />
          </div>
          <button
            type="button"
            onClick={() => setNight(!night)}
            aria-pressed={night}
            title="Red night-vision colours, so your eyes stay dark-adapted"
            className={`rounded-full px-3 py-1 text-xs ring-1 ring-line ${night ? "bg-accent text-bg" : "text-muted hover:text-fg"}`}
          >
            Night
          </button>
        </header>
        <div className="aspect-square w-full lg:aspect-auto lg:min-h-0 lg:flex-1">
          <DomeView path={path} zoomPath={status?.zoom_path ?? "dome-zoom"} edgeDeg={status?.edge_zenith_deg ?? 100}
            mask={maskEdit ? { alt: maskEdit, onChange: setMaskEdit } : null} />
        </div>
      </main>

      <aside className="flex flex-col border-line bg-panel lg:w-[380px] lg:border-l">
        <div className="border-b border-line p-4">
          <ModeSwitch mode={status?.mode} until={status?.smooth_until ?? null} busy={switching} onChange={setMode} />
        </div>
        <div className="border-b border-line p-4">
          <GroundMask status={status} editing={maskEdit} onEdit={setMaskEdit} />
        </div>
        <div className="flex border-b border-line" role="tablist">
          {(["cameras", "health"] as Tab[]).map((t) => (
            <button key={t} type="button" role="tab" aria-selected={tab === t} onClick={() => setTab(t)}
              className={`flex-1 py-2.5 text-sm capitalize ${tab === t ? "border-b-2 border-accent text-fg" : "text-muted hover:text-fg"}`}>
              {t === "cameras" ? "Camera settings" : "Health"}
            </button>
          ))}
        </div>
        <div className="p-4 lg:min-h-0 lg:flex-1 lg:overflow-y-auto">
          {tab === "cameras" ? <SettingsPanel /> : <HealthPanel status={status} />}
        </div>
      </aside>
    </div>
  );
}

function LiveBadge({ online, running, fps }: { online: boolean; running: boolean; fps?: number }) {
  const [dot, text] = !online
    ? ["bg-bad", "Server offline"]
    : running
      ? ["bg-good", `Live · ${fps?.toFixed(0)} fps`]
      : ["bg-warn", "Dome stopped"];
  return (
    <span className="flex items-center gap-1.5 rounded-full bg-raised px-2.5 py-0.5 text-xs text-muted ring-1 ring-line">
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
    ["detail", "Detail", "5120² · 15 fps"],
    ["smooth", "Smooth", "2048² · 30 fps"],
  ];
  return (
    <div>
      <div className="grid grid-cols-2 gap-1 rounded-xl bg-raised p-1 ring-1 ring-line" role="radiogroup" aria-label="Dome mode">
        {options.map(([m, label, sub]) => (
          <button key={m} type="button" role="radio" aria-checked={mode === m} disabled={busy || !mode}
            onClick={() => onChange(m)}
            className={`rounded-lg px-3 py-1.5 text-left transition-colors ${mode === m ? "bg-accent text-bg" : "text-muted hover:text-fg"}`}>
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
