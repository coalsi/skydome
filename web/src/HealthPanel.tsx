import type { Status } from "./api";

const ORDER = ["zenith", "north", "east", "south", "west"];

export function HealthPanel({ status }: { status: Status | null }) {
  if (!status) return <p className="text-sm text-muted">Waiting for the server…</p>;
  const live = status.live;
  const sys = status.system;
  const target = status.mode === "smooth" ? 30 : 15;
  const cams = live ? Object.entries(live.cameras).sort(([a], [b]) => ORDER.indexOf(a) - ORDER.indexOf(b)) : [];
  const temps = Object.entries(sys.temps_c).filter(([n]) => ["cpu", "gpu", "tj", "soc0"].includes(n));
  const hottest = Math.max(...Object.values(sys.temps_c), 0);

  return (
    <div className="space-y-5">
      <section>
        <h3 className="mb-2 text-[11px] font-semibold tracking-wider text-muted uppercase">Live dome</h3>
        {live ? (
          <>
            <div className="grid grid-cols-3 gap-2">
              <Stat label="Output" value={`${live.output_fps.toFixed(1)} fps`} tone={tone(live.output_fps, target)} />
              <Stat label="Stitch" value={`${live.stitch_ms.toFixed(1)} ms`} />
              <Stat label="Viewers" value={status.stream.viewers ?? "–"} />
            </div>
            <table className="mt-3 w-full text-sm">
              <thead>
                <tr className="text-left text-[11px] text-muted">
                  <th className="pb-1 font-medium">Camera</th>
                  <th className="pb-1 text-right font-medium">fps</th>
                  <th className="pb-1 text-right font-medium" title="Brightness correction that matches this camera to its neighbours">
                    Match
                  </th>
                  <th className="pb-1 text-right font-medium" title="Times the camera was reopened">Restarts</th>
                  <th className="pb-1 text-right font-medium" title="Damaged frames dropped">Dropped</th>
                </tr>
              </thead>
              <tbody className="tabular">
                {cams.map(([name, c]) => (
                  <tr key={name} className="border-t border-line">
                    <td className="py-1.5">
                      <span className={`mr-2 inline-block h-2 w-2 rounded-full ${c.state === "live" ? "bg-good" : "bg-bad"}`}
                        title={c.state} />
                      <span className="capitalize">{name}</span>
                      {c.state !== "live" && <span className="ml-1.5 text-xs text-bad">{c.state}</span>}
                    </td>
                    <td className={`text-right ${toneText(c.fps, target)}`}>{c.fps.toFixed(1)}</td>
                    <td className="text-right text-muted"
                      title={c.balance ? `colour shift U ${c.balance.u.toFixed(1)}, V ${c.balance.v.toFixed(1)}` : "off"}>
                      {c.balance ? `×${c.balance.gain.toFixed(2)}` : "–"}
                    </td>
                    <td className="text-right text-muted">{c.restarts}</td>
                    <td className="text-right text-muted">{c.damaged_dropped}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {live.encoder !== "running" && <p className="mt-2 text-xs text-bad">Encoder: {live.encoder}</p>}
            {live.balance_points !== undefined && (
              <p className="mt-2 text-xs leading-relaxed text-muted">
                {live.balance_points > 0
                  ? `Cameras matched to each other from ${live.balance_points.toLocaleString()} overlap points.`
                  : "Camera matching is holding its last values (too dark or too little overlap to measure)."}
              </p>
            )}
          </>
        ) : (
          <p className="text-sm text-muted">Not running.</p>
        )}
      </section>

      <section>
        <h3 className="mb-2 text-[11px] font-semibold tracking-wider text-muted uppercase">Jetson</h3>
        <div className="grid grid-cols-3 gap-2">
          {temps.map(([n, t]) => (
            <Stat key={n} label={n.toUpperCase()} value={`${t.toFixed(0)} °C`} tone={t >= 85 ? "bad" : t >= 75 ? "warn" : undefined} />
          ))}
          <Stat label="CPU" value={sys.cpu_pct == null ? "–" : `${sys.cpu_pct.toFixed(0)}%`} />
          <Stat label="Memory" value={sys.memory ? `${((sys.memory.used / sys.memory.total) * 100).toFixed(0)}%` : "–"} />
          <Stat label="Disk free" value={sys.disk ? gb(sys.disk.free) : "–"} />
          <Stat label="Wi-Fi" value={sys.wifi_dbm == null ? "wired" : `${sys.wifi_dbm} dBm`}
            tone={sys.wifi_dbm != null && sys.wifi_dbm < -75 ? "warn" : undefined} />
          <Stat label="Up" value={sys.uptime_s == null ? "–" : duration(sys.uptime_s)} />
        </div>
        {hottest >= 85 && <p className="mt-2 text-xs text-bad">Running hot: check the vents and the sun on the box.</p>}
      </section>
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: React.ReactNode; tone?: "warn" | "bad" }) {
  return (
    <div className="rounded-lg bg-raised px-2.5 py-2 ring-1 ring-line">
      <div className="text-[10px] tracking-wide text-muted uppercase">{label}</div>
      <div className={`tabular text-sm font-medium ${tone === "bad" ? "text-bad" : tone === "warn" ? "text-warn" : ""}`}>
        {value}
      </div>
    </div>
  );
}

const tone = (fps: number, target: number) => (fps < target * 0.6 ? "bad" : fps < target * 0.95 ? "warn" : undefined);
function toneText(fps: number, target: number) {
  const t = tone(fps, target);
  return t === "bad" ? "text-bad" : t === "warn" ? "text-warn" : "";
}
const gb = (b: number) => `${(b / 1e9).toFixed(b < 1e10 ? 1 : 0)} GB`;

function duration(s: number) {
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}
