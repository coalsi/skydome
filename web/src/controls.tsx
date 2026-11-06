import { useEffect, useRef, useState } from "react";

// A phone on its side: the picture takes the whole screen by itself
export const PHONE_SIDEWAYS = "(orientation: landscape) and (max-height: 540px)";

export function useMedia(query: string): boolean {
  const [on, setOn] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const list = window.matchMedia(query);
    const update = () => setOn(list.matches);
    update();
    list.addEventListener("change", update);
    return () => list.removeEventListener("change", update);
  }, [query]);
  return on;
}

// A slider that also answers a tap on its track: iOS only moves one by its thumb.
// Only a tap, so a thumb scrolling the panel past a slider doesn't change it
export function Range({ value, min, max, step, disabled, id, onChange }: {
  value: number; min: number; max: number; step: number; disabled?: boolean; id?: string;
  onChange: (v: number) => void;
}) {
  const down = useRef<{ x: number; y: number; t: number } | null>(null);
  return (
    <input id={id} type="range" min={min} max={max} step={step} value={value} disabled={disabled}
      onChange={(e) => onChange(Number(e.target.value))}
      onPointerDown={(e) => {
        down.current = e.pointerType === "touch" ? { x: e.clientX, y: e.clientY, t: performance.now() } : null;
      }}
      onPointerUp={(e) => {
        const d = down.current;
        down.current = null;
        if (!d || disabled || performance.now() - d.t > 400 || Math.hypot(e.clientX - d.x, e.clientY - d.y) > 8) return;
        const r = e.currentTarget.getBoundingClientRect();
        const f = Math.min(1, Math.max(0, (e.clientX - r.left - 14) / (r.width - 28)));   // 14: half the thumb
        onChange(min + Math.round((f * (max - min)) / step) * step);
      }} />
  );
}

export function Switch({ on, label, disabled, id, onChange }: {
  on: boolean; label?: string; disabled?: boolean; id?: string; onChange: (on: boolean) => void;
}) {
  return (
    <button id={id} type="button" role="switch" aria-checked={on} aria-label={label} disabled={disabled}
      onClick={() => onChange(!on)}
      className="flex h-11 w-14 shrink-0 items-center justify-end disabled:opacity-40">
      <span className={`relative h-7 w-12 rounded-full transition-colors ${on ? "bg-accent" : "bg-line"}`}>
        <span className={`absolute top-0.5 left-0.5 h-6 w-6 rounded-full bg-fg shadow transition-transform duration-150 ${on ? "translate-x-5" : ""}`} />
      </span>
    </button>
  );
}
