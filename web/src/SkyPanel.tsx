import { useEffect, useRef, useState } from "react";
import { api, type Stretch } from "./api";

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
          ? "Night: 1 s exposures stacked over 16 s, full gain, stars lifted. Give it ~16 s to build up."
          : "Day: the cameras' automatic exposure, no stacking or stretch.");
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

  const btn = "flex-1 rounded-lg px-3 py-1.5 text-sm ring-1 ring-line disabled:opacity-50";
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
      {note && <p className="mt-2 text-xs leading-relaxed text-muted">{note}</p>}
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
      <input id={id} type="range" min={min} max={max} step={step} value={value} disabled={disabled}
        onChange={(e) => onChange(Number(e.target.value))} />
    </div>
  );
}
