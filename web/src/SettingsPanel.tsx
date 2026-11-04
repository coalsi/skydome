import { useEffect, useRef, useState } from "react";
import { api, type Control } from "./api";

// How each V4L2 control is shown. Units are the cameras' own (exposure counts 100 µs)
type Kind = "slider" | "log" | "toggle" | "select";
interface Meta {
  label: string;
  group: "Exposure" | "Colour" | "Picture";
  kind?: Kind;
  on?: number; // toggle: value meaning "on"
  off?: number;
  options?: [number, string][];
  fmt?: (v: number) => string;
  needsOff?: string; // only adjustable while this toggle is off
}

const META: Record<string, Meta> = {
  auto_exposure: { label: "Auto exposure", group: "Exposure", kind: "toggle", on: 3, off: 1 },
  exposure_time_absolute: {
    label: "Exposure time", group: "Exposure", kind: "log", needsOff: "auto_exposure",
    fmt: (v) => (v >= 10000 ? "1 s" : `${(v / 10).toFixed(v < 100 ? 1 : 0)} ms`),
  },
  gain: { label: "Gain", group: "Exposure" },
  backlight_compensation: { label: "Backlight compensation", group: "Exposure" },
  white_balance_automatic: { label: "Auto white balance", group: "Colour", kind: "toggle", on: 1, off: 0 },
  white_balance_temperature: {
    label: "Colour temperature", group: "Colour", needsOff: "white_balance_automatic", fmt: (v) => `${v} K`,
  },
  saturation: { label: "Saturation", group: "Colour" },
  hue: { label: "Hue", group: "Colour" },
  brightness: { label: "Brightness", group: "Picture" },
  contrast: { label: "Contrast", group: "Picture" },
  gamma: { label: "Gamma", group: "Picture" },
  sharpness: { label: "Sharpness", group: "Picture" },
  power_line_frequency: {
    label: "Mains flicker", group: "Picture", kind: "select",
    options: [[0, "Off"], [1, "50 Hz"], [2, "60 Hz"]],
  },
};
const GROUPS = ["Exposure", "Colour", "Picture"] as const;
const KNOWN = Object.keys(META);
const order = (name: string) => (KNOWN.includes(name) ? KNOWN.indexOf(name) : KNOWN.length);

export function SettingsPanel() {
  const [controls, setControls] = useState<Control[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const pending = useRef<Record<string, number>>({});
  const timer = useRef<number>();

  const load = () =>
    api.settings().then((r) => setControls(r.controls)).catch((e) => setError(String(e.message ?? e)));

  // Pick up changes made elsewhere (ONVIF, another browser)
  useEffect(() => {
    load();
    const t = window.setInterval(() => !Object.keys(pending.current).length && load(), 15000);
    return () => window.clearInterval(t);
  }, []);

  // Show the new value at once; send a burst of slider moves as one change
  const change = (name: string, value: number) => {
    setControls((cs) => cs && cs.map((c) => (c.name === name ? { ...c, value, set: true } : c)));
    pending.current[name] = value;
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => {
      const changes = pending.current;
      pending.current = {};
      api
        .patchSettings(changes)
        .then((r) => {
          setError(null);
          if (!Object.keys(pending.current).length) setControls(r.controls);
        })
        .catch((e) => {
          setError(String(e.message ?? e));
          load();
        });
    }, 150);
  };

  if (!controls) return <p className="text-sm text-muted">{error ?? "Loading camera settings…"}</p>;
  const byName = Object.fromEntries(controls.map((c) => [c.name, c]));
  const isOn = (name: string) => byName[name] && byName[name].value === META[name].on;

  return (
    <div className="space-y-5">
      <p className="text-xs leading-relaxed text-muted">
        These apply to all five cameras together, are kept across restarts, and are the same settings an NVR changes
        over ONVIF.
      </p>
      {error && <p className="rounded-md bg-bad/10 px-3 py-2 text-xs text-bad ring-1 ring-bad/30">{error}</p>}
      {GROUPS.map((group) => {
        const items = controls
          .filter((c) => META[c.name] && META[c.name].group === group)
          .sort((a, b) => order(a.name) - order(b.name));
        if (!items.length) return null;
        return (
          <section key={group}>
            <h3 className="mb-2 text-[11px] font-semibold tracking-wider text-muted uppercase">{group}</h3>
            <div className="space-y-3">
              {items.map((c) => (
                <ControlRow
                  key={c.name}
                  c={c}
                  meta={META[c.name]}
                  disabled={!!META[c.name]?.needsOff && !!isOn(META[c.name].needsOff!)}
                  onChange={(v) => change(c.name, v)}
                />
              ))}
            </div>
          </section>
        );
      })}
    </div>
  );
}

function ControlRow({ c, meta, disabled, onChange }: {
  c: Control; meta: Meta; disabled: boolean; onChange: (v: number) => void;
}) {
  const kind = meta.kind ?? "slider";
  const id = `ctl-${c.name}`;
  const reset = c.value !== c.default && !disabled && (
    <button type="button" onClick={() => onChange(c.default)} className="text-[11px] text-muted hover:text-accent"
      title={`Back to the camera's default (${meta.fmt ? meta.fmt(c.default) : c.default})`}>
      reset
    </button>
  );

  if (kind === "toggle") {
    const on = c.value === meta.on;
    return (
      <div className="flex items-center justify-between gap-3">
        <label htmlFor={id} className="text-sm">{meta.label}</label>
        <button id={id} type="button" role="switch" aria-checked={on}
          onClick={() => onChange(on ? meta.off! : meta.on!)}
          className={`relative h-6 w-11 shrink-0 rounded-full transition-colors ${on ? "bg-accent" : "bg-line"}`}>
          <span className={`absolute top-0.5 left-0.5 h-5 w-5 rounded-full bg-fg shadow transition-transform ${on ? "translate-x-5" : ""}`} />
        </button>
      </div>
    );
  }

  if (kind === "select") {
    return (
      <div className="flex items-center justify-between gap-3">
        <label htmlFor={id} className="text-sm">{meta.label}</label>
        <div className="flex rounded-lg bg-raised p-0.5 ring-1 ring-line" role="radiogroup" aria-label={meta.label}>
          {meta.options!.map(([v, label]) => (
            <button key={v} type="button" role="radio" aria-checked={c.value === v} onClick={() => onChange(v)}
              className={`rounded-md px-2.5 py-1 text-xs ${c.value === v ? "bg-accent text-bg" : "text-muted hover:text-fg"}`}>
              {label}
            </button>
          ))}
        </div>
      </div>
    );
  }

  // Exposure spans 0.1 ms to 1 s: a log slider gives each decade the same room
  const log = kind === "log";
  const lo = Math.log(Math.max(1, c.min));
  const hi = Math.log(c.max);
  const toPos = (v: number) => (log ? Math.round(((Math.log(Math.max(1, v)) - lo) / (hi - lo)) * 1000) : v);
  const fromPos = (p: number) => (log ? Math.round(Math.exp(lo + (p / 1000) * (hi - lo))) : p);
  return (
    <div className={disabled ? "opacity-60" : ""}>
      <div className="mb-0.5 flex items-baseline justify-between gap-3">
        <label htmlFor={id} className="text-sm">{meta.label}</label>
        <span className="flex items-baseline gap-2">
          {reset}
          <span className="tabular text-xs text-muted">
            {disabled ? "auto" : meta.fmt ? meta.fmt(c.value) : c.value}
          </span>
        </span>
      </div>
      <input id={id} type="range" disabled={disabled}
        min={log ? 0 : c.min} max={log ? 1000 : c.max} step={log ? 1 : c.step}
        value={toPos(c.value)} onChange={(e) => onChange(fromPos(Number(e.target.value)))} />
    </div>
  );
}
