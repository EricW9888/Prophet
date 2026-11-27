export type BusinessMapDriver = { kind: string; label: string; revenue: number; share: number };

export type BusinessMap = {
  entity_id: string;
  period_start: string;
  period_end: string;
  total_revenue: number;
  filing_url: string;
  drivers: BusinessMapDriver[];
};

export type BusinessMapGroup = {
  kind: string;
  title: string;
  drivers: BusinessMapDriver[];
  /** Share of total revenue this breakdown does not account for, when it is a meaningful part. */
  notBrokenOut: number | null;
};

const GROUPS: Array<[string, string]> = [
  ["segment", "By segment"],
  ["product", "By product or service"],
  ["region", "By region"],
  ["customer", "Major customers"],
];

/** A filing's breakdowns in reading order, each sorted by share, largest first. */
export function businessMapGroups(map: BusinessMap): BusinessMapGroup[] {
  return GROUPS.flatMap(([kind, title]) => {
    const drivers = map.drivers.filter((driver) => driver.kind === kind).sort((a, b) => b.share - a.share);
    // A single row covering the whole company (one reportable segment) says nothing.
    if (!drivers.length || (drivers.length === 1 && drivers[0].share >= 0.99)) return [];
    const covered = drivers.reduce((sum, driver) => sum + driver.share, 0);
    return [{ kind, title, drivers, notBrokenOut: covered < 0.95 ? Math.max(0, 1 - covered) : null }];
  });
}

/** $412M, $3.1B, $1.2T: enough precision to compare drivers at a glance. */
export function compactDollars(value: number): string {
  const abs = Math.abs(value);
  const [divisor, suffix] = abs >= 1e12 ? [1e12, "T"] : abs >= 1e9 ? [1e9, "B"] : abs >= 1e6 ? [1e6, "M"] : [1e3, "K"];
  const scaled = value / divisor;
  return `$${scaled.toFixed(Math.abs(scaled) >= 100 ? 0 : 1)}${suffix}`;
}

export function sharePercent(share: number): string {
  const pct = share * 100;
  return pct > 0 && pct < 1 ? "<1%" : `${Math.round(pct)}%`;
}
