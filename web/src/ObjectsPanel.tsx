import { useEffect, useState } from "react";
import { api, type ObjectSummary, type SkyObject } from "./api";

// The sky watcher's log (D-030): what moved through the sky, what it was, and
// your corrections, which it learns from. Tap one to see its path on the dome.

type Filter = { key: string; label: string; q: { kind?: string; unknown?: boolean } };
const FILTERS: Filter[] = [
  { key: "all", label: "All", q: {} },
  { key: "unknown", label: "Unknown", q: { unknown: true } },
  { key: "plane", label: "Planes", q: { kind: "plane" } },
  { key: "satellite", label: "Satellites", q: { kind: "satellite" } },
  { key: "meteor", label: "Meteors", q: { kind: "meteor" } },
];

const TONE: Record<string, string> = {
  unknown: "bg-bad/15 text-bad ring-bad/30",
  plane: "bg-warn/15 text-warn ring-warn/30",
  helicopter: "bg-warn/15 text-warn ring-warn/30",
  satellite: "bg-accent/15 text-accent ring-accent/30",
  meteor: "bg-good/15 text-good ring-good/30",
};

export function ObjectsPanel({ selected, onSelect }: {
  selected: number | null; onSelect: (o: SkyObject | null) => void;
}) {
  const [filter, setFilter] = useState("all");
  const [objects, setObjects] = useState<SkyObject[] | null>(null);
  const [labels, setLabels] = useState<string[]>([]);
  const [summary, setSummary] = useState<ObjectSummary | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let stop = false;
    const q = FILTERS.find((f) => f.key === filter)!.q;
    const load = () => {
      api.objects(q)
        .then((r) => !stop && (setObjects(r.objects), setLabels(r.labels), setError(null)))
        .catch((e) => !stop && setError(String(e.message ?? e)));
      api.objectSummary().then((s) => !stop && setSummary(s)).catch(() => {});
    };
    load();
    const t = window.setInterval(load, 10000);
    return () => {
      stop = true;
      window.clearInterval(t);
    };
  }, [filter]);

  const relabel = (o: SkyObject, value: string) => {
    let label: string | null = value || null;
    if (value === "__other") {
      label = window.prompt("What was it? (a word or two)")?.trim() || null;
      if (!label) return;
    }
    api.labelObject(o.id, label)
      .then((n) => {
        setObjects((os) => os && os.map((x) => (x.id === n.id ? n : x)));
        if (label && !labels.includes(label)) setLabels([...labels, label]);
        if (selected === n.id) onSelect(n);
      })
      .catch((e) => setError(String(e.message ?? e)));
  };

  return (
    <div className="space-y-3">
      {summary && (
        <p className="text-xs leading-relaxed text-muted">
          Last 24 h: {Object.entries(summary.counts).sort((a, b) => b[1] - a[1])
            .map(([k, n]) => `${n} ${k}`).join(" · ") || "nothing yet"}.{" "}
          {summary.labelled ? `Learning from ${summary.labelled} of your labels.` : "Correct a label to teach it."}
        </p>
      )}
      <div className="flex flex-wrap gap-1" role="radiogroup" aria-label="Show">
        {FILTERS.map((f) => (
          <button key={f.key} type="button" role="radio" aria-checked={filter === f.key} onClick={() => setFilter(f.key)}
            className={`press min-h-9 rounded-full px-3 text-sm ring-1 ring-line ${filter === f.key ? "bg-accent text-bg" : "text-muted hover:text-fg"}`}>
            {f.label}
            {f.key === "unknown" && summary?.unknown ? <span className="ml-1 font-semibold">{summary.unknown}</span> : null}
          </button>
        ))}
      </div>
      {error && <p className="rounded-md bg-bad/10 px-3 py-2 text-xs text-bad ring-1 ring-bad/30">{error}</p>}
      {!objects ? (
        <p className="text-sm text-muted">Loading…</p>
      ) : !objects.length ? (
        <p className="text-sm text-muted">Nothing here yet. The watcher logs everything that moves across the sky.</p>
      ) : (
        <ul className="space-y-2">
          {objects.map((o) => (
            <ObjectRow key={o.id} o={o} labels={labels} open={selected === o.id}
              onOpen={() => onSelect(selected === o.id ? null : o)} onLabel={(v) => relabel(o, v)} />
          ))}
        </ul>
      )}
    </div>
  );
}

function ObjectRow({ o, labels, open, onOpen, onLabel }: {
  o: SkyObject; labels: string[]; open: boolean; onOpen: () => void; onLabel: (v: string) => void;
}) {
  const when = new Date(o.start * 1000);
  const dur = o.end - o.start;
  const first = o.path[0], last = o.path[o.path.length - 1];
  return (
    <li id={`object-${o.id}`} className={`rounded-lg bg-raised ring-1 ${open ? "ring-accent" : "ring-line"}`}>
      <button type="button" onClick={onOpen} className="flex w-full gap-3 p-2 text-left">
        {o.thumb ? (
          <img src={open && o.clip ? o.clip : o.thumb} alt="" loading="lazy"
            className={`shrink-0 rounded-md bg-bg object-cover ${open ? "h-28 w-28" : "h-14 w-14"}`} />
        ) : <div className="h-14 w-14 shrink-0 rounded-md bg-bg" />}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className={`rounded-full px-2 py-0.5 text-[11px] font-medium capitalize ring-1 ${TONE[o.kind] ?? "bg-line/40 text-fg ring-line"}`}>
              {o.kind}
            </span>
            {o.match && <span className="truncate text-sm font-medium">{o.match}</span>}
            {o.shower && <span className="text-xs text-muted">{o.shower}</span>}
          </div>
          <div className="tabular mt-0.5 text-xs text-muted">
            {when.toLocaleDateString(undefined, { month: "short", day: "numeric" })}{" "}
            {when.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
            {" · "}{dur < 60 ? `${dur.toFixed(1)} s` : `${Math.round(dur / 60)} min`}
            {first && ` · ${Math.round(first[2])}° → ${Math.round(last[2])}° up`}
          </div>
          <div className="mt-0.5 text-xs text-muted">{why(o)}</div>
        </div>
      </button>
      <div className="flex items-center gap-2 border-t border-line px-2 py-1.5">
        <label htmlFor={`label-${o.id}`} className="text-xs text-muted">It was</label>
        <select id={`label-${o.id}`} value={o.label ?? ""} onChange={(e) => onLabel(e.target.value)}
          className="min-h-10 min-w-0 flex-1 rounded-md bg-bg px-2 ring-1 ring-line">
          <option value="">{`${o.auto_kind} (the watcher's guess)`}</option>
          {labels.map((l) => <option key={l} value={l}>{l}</option>)}
          <option value="__other">something else…</option>
        </select>
        {o.label && <span className="text-xs text-good" title="Your label: it learns from this">✓ yours</span>}
      </div>
    </li>
  );
}

function why(o: SkyObject): string {
  const d = o.detail ?? {};
  if (o.source === "adsb") {
    const ft = Number(d.alt_ft);
    return `ADS-B: ${o.match}${ft ? ` at ${ft.toLocaleString()} ft` : ""}${d.range_km ? `, ${d.range_km} km away` : ""}`;
  }
  if (o.source === "tle") return `Satellite orbit: ${o.match}${d.range_km ? `, ${Number(d.range_km).toLocaleString()} km up` : ""}`;
  if (o.label) return `You said ${o.label}${o.label !== o.auto_kind ? ` (it guessed ${o.auto_kind})` : ""}`;
  if (o.source === "learned") return `Like what you've labelled ${o.auto_kind} (${Math.round(o.confidence * 100)}% sure)`;
  return o.kind === "unknown" ? "No aircraft, satellite or known thing matches it" : `From its motion (${Math.round(o.confidence * 100)}% sure)`;
}
