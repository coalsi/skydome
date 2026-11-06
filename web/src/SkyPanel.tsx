import { useEffect, useRef, useState } from "react";
import { api, type Calibration, type Stretch } from "./api";
import { Range } from "./controls";

// Night / Day in one click, and the night stretch that lifts faint stars (D-025)
export function SkyPanel() {
  const [stretch, setStretch] = useState<Stretch | null>(null);
  const [stack, setStack] = useState<number | null>(null);
  const stackTimer = useRef<number>();
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const timer = useRef<number>();

  useEffect(() => {
    api.stretch().then(setStretch).catch(() => {});
    api.stack().then((s) => setStack(s.frames)).catch(() => {});
  }, []);

  const preset = (name: "night" | "day") => {
    setBusy(name);
    setNote(null);
    api
      .preset(name)
      .then((r) => {
        setStretch(r.stretch);
        if (r.stack !== undefined) setStack(r.stack);
        setNote(name === "night"
          ? "Night: long exposures, a few frames stacked, faint stars lifted. Give it a minute to build up."
          : "Day: a normal picture, no stacking or stretch.");
      })
      .catch((e) => setNote(String(e.message ?? e)))
      .finally(() => setBusy(null));
  };

  // Show slider moves at once, send them a moment later
  const change = (next: Stretch) => {
    setStretch(next);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => api.setStretch(next).catch(() => {}), 150);
  };

  const btn = "press min-h-11 flex-1 rounded-lg px-3 text-sm ring-1 ring-line disabled:opacity-50";
  return (
    <div>
      <div className="mb-2 text-sm">Sky</div>
      <div className="flex gap-2">
        <button type="button" className={`${btn} hover:text-accent`} disabled={!!busy} onClick={() => preset("night")}>
          {busy === "night" ? "Setting…" : "Night (stars)"}
        </button>
        <button type="button" className={`${btn} hover:text-accent`} disabled={!!busy} onClick={() => preset("day")}>
          {busy === "day" ? "Setting…" : "Day"}
        </button>
      </div>
      <p className="mt-2 text-xs leading-relaxed text-muted">
        {note ?? "Switches by itself at dusk and dawn. These buttons are to do it sooner."}
      </p>
      {stretch && (
        <div className="mt-3 space-y-2">
          {stack !== null && (
            <Slider label="Stacking" value={stack} min={0} max={60} step={1}
              fmt={(v) => (v < 2 ? "off" : `${v} frames`)}
              onChange={(v) => {
                setStack(v);
                window.clearTimeout(stackTimer.current);
                stackTimer.current = window.setTimeout(() => api.setStack(v).catch(() => {}), 400);
              }} />
          )}
          <Slider label="Star stretch" value={stretch.k} min={0} max={40} step={1}
            fmt={(v) => (v === 0 ? "off" : `${v}`)} onChange={(k) => change({ ...stretch, k })} />
          <Slider label="Black point" value={stretch.black} min={0} max={80} step={1} disabled={stretch.k === 0}
            fmt={(v) => `${v}`} onChange={(black) => change({ ...stretch, black })} />
        </div>
      )}
      <StarCalibration />
    </div>
  );
}

// Calibrating from the stars (D-028): which way is north, where every star
// is. Runs by itself after dark; the button runs it now
function StarCalibration() {
  const [cal, setCal] = useState<Calibration | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let t: number;
    const load = () =>
      api.calibration()
        .then((c) => {
          setCal(c);
          t = window.setTimeout(load, c.state === "running" ? 3000 : 30000);
        })
        .catch(() => (t = window.setTimeout(load, 30000)));
    load();
    return () => window.clearTimeout(t);
  }, []);
  if (!cal) return null;

  const run = () => {
    setErr(null);
    api.calibrate().then(setCal).catch((e) => setErr(String(e.message ?? e)));
  };
  const when = (iso?: string) =>
    iso ? new Date(iso).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" }) : "";
  const running = cal.state === "running";
  let line: string;
  if (running) line = `Calibrating from the stars… ${cal.message ?? ""}`;
  else if (cal.state === "done") {
    const turned = cal.heading_deg !== undefined && Math.abs(cal.heading_deg) >= 0.05
      ? ` The rig had turned ${Math.abs(cal.heading_deg).toFixed(1)}° ${cal.heading_deg > 0 ? "clockwise" : "anticlockwise"}` +
        `${cal.tilt_deg && cal.tilt_deg >= 0.1 ? ` and tipped ${cal.tilt_deg.toFixed(1)}°` : ""}.`
      : "";
    line = `Aligned to ${cal.stars ?? 0} stars, ${when(cal.utc ?? cal.started_utc)}.` +
      (cal.applied ? `${turned} Updated; the dome restarted with it.` : " Nothing had moved.");
  } else if (cal.state === "failed") line = `Last try ${when(cal.started_utc)}: ${cal.message ?? "failed"}. It tries again in 20 minutes after dark.`;
  else line = "Not calibrated from the stars yet.";

  return (
    <div className="mt-4 border-t border-line pt-3">
      <div className="mb-1 text-sm">Find north from the stars</div>
      <p className="text-xs leading-relaxed text-muted">{line}</p>
      {cal.state === "done" && cal.cameras && (
        <p className="tabular mt-1 text-xs text-muted">
          {Object.entries(cal.cameras).map(([n, c]) => `${n} ${c.own ? c.stars : "–"}`).join(" · ")}
        </p>
      )}
      <button type="button" disabled={running || !cal.dark_enough} onClick={run}
        title={cal.dark_enough ? "Match the cameras to the stars now (a few minutes)"
          : `Needs a dark sky: the Sun is at ${Math.round(cal.sun_alt)}°`}
        className="press mt-2 min-h-11 w-full rounded-lg px-3 text-sm ring-1 ring-line hover:text-accent disabled:opacity-50">
        {running ? "Calibrating…" : cal.dark_enough ? "Calibrate from the stars" : "Calibrate (after dark)"}
      </button>
      <label className="mt-1 flex min-h-11 items-center gap-3 text-xs text-muted">
        <input type="checkbox" className="h-5 w-5 shrink-0" checked={cal.auto}
          onChange={(e) => api.setCalibrationAuto(e.target.checked).then(setCal).catch(() => {})} />
        Every night by itself, and after each start-up once it's dark
      </label>
      {err && <p className="mt-1 text-xs text-red-400">{err}</p>}
    </div>
  );
}

function Slider({ label, value, min, max, step, fmt, onChange, disabled }: {
  label: string; value: number; min: number; max: number; step: number;
  fmt: (v: number) => string; onChange: (v: number) => void; disabled?: boolean;
}) {
  const id = `sky-${label.replace(/\s/g, "-")}`;
  return (
    <div className={disabled ? "opacity-60" : ""}>
      <div className="mb-0.5 flex items-baseline justify-between">
        <label htmlFor={id} className="text-sm">{label}</label>
        <span className="tabular text-xs text-muted">{fmt(value)}</span>
      </div>
      <Range id={id} min={min} max={max} step={step} value={value} disabled={disabled} onChange={onChange} />
    </div>
  );
}
