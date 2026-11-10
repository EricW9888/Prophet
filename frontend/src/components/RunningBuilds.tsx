"use client";

import { useEffect, useState } from "react";
import { API_BASE } from "@/lib/api";

async function readBuild(url: string, signal: AbortSignal): Promise<string | null> {
  try {
    const response = await fetch(url, { cache: "no-store", signal });
    if (!response.ok) return null;
    const { build } = await response.json();
    return typeof build === "string" && /^[0-9a-f]{64}$/.test(build) ? build : null;
  } catch {
    return null;
  }
}

export default function RunningBuilds() {
  const [builds, setBuilds] = useState<(string | null)[]>([]);
  useEffect(() => {
    const controller = new AbortController();
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(5000)]);
    void Promise.all([readBuild("/build-info", signal), readBuild(`${API_BASE}/runtime-build`, signal)]).then((values) => {
      if (!controller.signal.aborted) setBuilds(values);
    });
    return () => controller.abort();
  }, []);
  return <section className="border border-line bg-panel p-4" aria-label="Running builds">
    <h2 className="text-sm font-semibold">Running builds</h2>
    <dl className="mt-2 flex flex-wrap gap-x-6 gap-y-2 text-xs" aria-live="polite">
      {["Frontend", "Backend"].map((label, index) => <div key={label} className="flex gap-2">
        <dt className="text-muted">{label}</dt><dd title={builds[index] ?? undefined}>{builds.length ? builds[index]?.slice(0, 12) ?? "Unknown" : "Checking…"}</dd>
      </div>)}
    </dl>
    <p className="mt-2 text-xs text-muted">Use <code>scripts/prophet.py status</code> in the local launcher to compare these builds with the checkout.</p>
  </section>;
}
