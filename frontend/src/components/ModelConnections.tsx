"use client";

export type ModelConnection = {
  name: string;
  base_url: string;
  model: string;
  roles: string[];
  enabled: boolean;
  rate_per_minute: number;
  max_concurrency: number;
  api_key_set?: boolean;
  api_key?: string;
};

const KINDS: Array<[string, string]> = [
  ["chat", "Quick replies"],
  ["bulk", "Reading documents"],
  ["planning", "Everything else"],
];

const inputClass = "w-full border border-line bg-panel px-3 py-2 text-sm text-foreground";

export function newConnection(existing: ModelConnection[]): ModelConnection {
  let n = existing.length + 1;
  while (existing.some((c) => c.name === `connection-${n}`)) n += 1;
  return { name: `connection-${n}`, base_url: "", model: "", roles: ["chat", "bulk", "planning"], enabled: true, rate_per_minute: 30, max_concurrency: 2 };
}

/** Extra OpenAI-compatible connections that take over when the main provider is cooling down (#299). */
export default function ModelConnections({ value, onChange }: { value: ModelConnection[]; onChange: (next: ModelConnection[]) => void }) {
  const update = (index: number, patch: Partial<ModelConnection>) => onChange(value.map((c, i) => (i === index ? { ...c, ...patch } : c)));
  return (
    <section aria-label="Extra model connections" className="space-y-3 border-t border-line pt-4">
      <div>
        <h3 className="text-sm font-semibold">Extra model connections</h3>
        <p className="mt-1 text-xs leading-5 text-muted">
          When the main provider is rate-limited or cooling down, work moves to these connections in order, for the kinds of work each one takes. Each has its own health and request limits.
          Any OpenAI-compatible service works (base URL ending in <code>/v1</code>). <strong className="font-semibold text-foreground">A connection can receive the same context as the main provider, including your portfolio and chats</strong>, so add only services whose terms you accept for that.
        </p>
      </div>
      {value.map((connection, index) => (
        <fieldset key={index} className="space-y-2 border border-line p-3">
          <legend className="px-1 text-xs text-muted">Connection {index + 1}</legend>
          <div className="grid gap-2 sm:grid-cols-2">
            <label className="text-xs text-muted">Name<input className={inputClass} value={connection.name} onChange={(e) => update(index, { name: e.target.value })} /></label>
            <label className="text-xs text-muted">Model<input className={inputClass} value={connection.model} placeholder="provider/model-name" onChange={(e) => update(index, { model: e.target.value })} /></label>
            <label className="text-xs text-muted sm:col-span-2">Base URL<input className={inputClass} value={connection.base_url} placeholder="https://api.example.com/v1" onChange={(e) => update(index, { base_url: e.target.value })} /></label>
            <label className="text-xs text-muted sm:col-span-2">API key<input type="password" autoComplete="off" className={inputClass} value={connection.api_key ?? ""} placeholder={connection.api_key_set ? "Leave blank to keep the saved key" : "Paste the service's API key"} onChange={(e) => update(index, { api_key: e.target.value })} /></label>
            <label className="text-xs text-muted">Requests per minute<input type="number" min={1} className={inputClass} value={connection.rate_per_minute} onChange={(e) => update(index, { rate_per_minute: Math.max(1, Number(e.target.value) || 1) })} /></label>
            <label className="text-xs text-muted">Requests at once<input type="number" min={1} className={inputClass} value={connection.max_concurrency} onChange={(e) => update(index, { max_concurrency: Math.max(1, Number(e.target.value) || 1) })} /></label>
          </div>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
            <span className="text-xs text-muted">Takes:</span>
            {KINDS.map(([kind, label]) => (
              <label key={kind} className="inline-flex items-center gap-1.5">
                <input type="checkbox" checked={connection.roles.includes(kind)} onChange={(e) => update(index, { roles: e.target.checked ? [...connection.roles, kind] : connection.roles.filter((r) => r !== kind) })} />
                {label}
              </label>
            ))}
          </div>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <label className="inline-flex items-center gap-1.5 text-sm"><input type="checkbox" checked={connection.enabled} onChange={(e) => update(index, { enabled: e.target.checked })} />On</label>
            <button type="button" className="workspace-control" onClick={() => onChange(value.filter((_, i) => i !== index))}>Remove</button>
          </div>
        </fieldset>
      ))}
      <button type="button" className="workspace-control" onClick={() => onChange([...value, newConnection(value)])}>Add connection</button>
    </section>
  );
}
