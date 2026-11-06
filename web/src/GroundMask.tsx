import { useState } from "react";
import { api, type Status } from "./api";
import { Switch } from "./controls";

// Hide the ground (houses, trees) below a horizon line: on/off, and an editor
// that paints the line on the dome (D-024)
export function GroundMask({ status, editing, onEdit }: {
  status: Status | null; editing: number[] | null; onEdit: (alt: number[] | null) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const gm = status?.ground_mask;
  const run = (p: Promise<unknown>) => {
    setBusy(true);
    setError(null);
    return p.catch((e) => setError(String(e.message ?? e))).finally(() => setBusy(false));
  };

  const startEdit = () =>
    run(api.groundMask().then((m) => onEdit(m.alt ?? new Array(360).fill(0))));
  const save = () => run(api.setGroundMask({ alt: editing!, on: true }).then(() => onEdit(null)));

  if (editing) {
    return (
      <div>
        <div className="mb-2 text-sm font-medium">Editing the ground mask</div>
        <p className="mb-3 text-xs leading-relaxed text-muted">
          Drag on the dome to move the red line: everything outside it is hidden. Zoom in (scroll or pinch) for
          detail. Saving turns the mask on.
        </p>
        <div className="flex gap-2">
          <button type="button" onClick={save} disabled={busy}
            className="press min-h-11 flex-1 rounded-lg bg-accent px-3 text-sm font-medium text-bg disabled:opacity-50">Save</button>
          <button type="button" onClick={() => onEdit(null)}
            className="press min-h-11 flex-1 rounded-lg px-3 text-sm text-muted ring-1 ring-line hover:text-fg">Cancel</button>
        </div>
        {error && <p className="mt-2 text-xs text-bad">{error}</p>}
      </div>
    );
  }

  const on = !!gm?.on;
  return (
    <div>
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="text-sm">Hide the ground</div>
          <div className="text-xs text-muted">Houses, trees and fences below the horizon line</div>
        </div>
        <Switch on={on} label="Hide the ground" disabled={busy || !gm?.available}
          onChange={(v) => run(api.setGroundMask({ on: v }))} />
      </div>
      <button type="button" onClick={startEdit} disabled={busy}
        className="press mt-1 min-h-11 text-sm text-accent hover:underline disabled:opacity-50">
        {gm?.available ? "Edit the line" : "Draw a horizon line"}
      </button>
      {error && <p className="mt-2 text-xs text-bad">{error}</p>}
    </div>
  );
}
