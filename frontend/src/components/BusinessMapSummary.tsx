"use client";

import { useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";
import { type BusinessMap, businessMapGroups, compactDollars, sharePercent } from "@/lib/business-map";

const SHOWN = 5;

/** Where a company's revenue comes from, per its latest annual report. Renders nothing when no map exists. */
export default function BusinessMapSummary({ entityId, className = "mt-3 border-t border-line pt-3" }: { entityId: string; className?: string }) {
  const [state, setState] = useState<{ entityId: string; map: BusinessMap | null } | null>(null);

  useEffect(() => {
    let current = true;
    apiFetch<BusinessMap | null>(`/business-map/${entityId}`)
      .then((map) => { if (current) setState({ entityId, map }); })
      .catch(() => { if (current) setState({ entityId, map: null }); });
    return () => { current = false; };
  }, [entityId]);

  const map = state?.entityId === entityId ? state.map : null;
  if (!map) return null;
  const yearEnd = new Date(`${map.period_end}T00:00:00`).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });

  return (
    <section aria-label="Where the revenue comes from" className={`space-y-3 text-sm ${className}`}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h4 className="font-semibold">Where the revenue comes from</h4>
        <a href={map.filing_url} target="_blank" rel="noreferrer" className="text-xs text-action underline">
          Annual report, year ended {yearEnd}
        </a>
      </div>
      <p className="text-xs text-muted">Total revenue {compactDollars(map.total_revenue)}. Each breakdown is the company&apos;s own, as tagged in its filing.</p>
      {businessMapGroups(map).map((group) => {
        const rows = (drivers: typeof group.drivers) => drivers.map((driver) => (
          <li key={driver.label} className="grid grid-cols-[minmax(0,1fr)_4.5rem_2.75rem] items-center gap-x-2">
            <span className="truncate" title={`${driver.label}: ${compactDollars(driver.revenue)}`}>{driver.label}</span>
            <span aria-hidden="true" className="h-1.5 bg-panel-muted">
              <span className="block h-full bg-muted/70" style={{ width: `${Math.max(2, Math.min(100, driver.share * 100))}%` }} />
            </span>
            <span className="text-right tabular-nums">{sharePercent(driver.share)}</span>
          </li>
        ));
        return (
          <div key={group.kind}>
            <h5 className="text-xs font-medium text-muted">{group.title}</h5>
            <ul className="mt-1 space-y-1">{rows(group.drivers.slice(0, SHOWN))}</ul>
            {group.drivers.length > SHOWN ? (
              <details className="mt-1">
                <summary className="cursor-pointer text-xs text-action">Show all {group.drivers.length}</summary>
                <ul className="mt-1 space-y-1">{rows(group.drivers.slice(SHOWN))}</ul>
              </details>
            ) : null}
            {group.notBrokenOut ? (
              <p className="mt-1 text-xs text-muted">{sharePercent(group.notBrokenOut)} of revenue is not broken out here.</p>
            ) : null}
          </div>
        );
      })}
    </section>
  );
}
