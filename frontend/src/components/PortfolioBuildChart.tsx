"use client";

import {
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Area,
  AreaChart,
  XAxis,
  YAxis,
} from "recharts";
import { type DashboardSummary } from "@/lib/api";
import { safeFormatCurrency } from "@/lib/formatting";

export default function PortfolioBuildChart({ points }: { points: DashboardSummary["portfolio_build_series"] }) {
  if (points.length === 0) return <div className="py-12 text-center text-muted italic text-sm">No series data</div>;


  return (
    <div className="mt-4 min-w-0">
      <ResponsiveContainer width="100%" height={180}>
        <AreaChart data={points}>
          <defs>
            <linearGradient id="colorDeployed" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor="var(--color-action)" stopOpacity={0.25}/>
              <stop offset="95%" stopColor="var(--color-action)" stopOpacity={0}/>
            </linearGradient>
          </defs>
          <CartesianGrid vertical={false} stroke="var(--color-line)" />
          <XAxis
            dataKey="as_of"
            tickFormatter={value => new Date(value).toLocaleDateString(undefined, {month: "short", day: "numeric", year: "2-digit"})}
            tick={{fill: "var(--color-muted)", fontSize: 12}} minTickGap={32} tickLine={false} axisLine={false}
          />
          <YAxis
            width="auto" domain={["auto", "auto"]} tickCount={4}
            tickFormatter={value => safeFormatCurrency(value)}
            tick={{fill: "var(--color-muted)", fontSize: 12}} tickLine={false} axisLine={false}
          />
          <Tooltip
            content={({ active, payload }) => {
              if (active && payload && payload.length) {
                const data = payload[0].payload;
                return (
                  <div className="border border-line bg-panel p-3">
                    <p className="mb-1 text-xs font-bold uppercase tracking-widest text-muted ">
                      {new Date(data.as_of).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })}
                    </p>
                    <p className="text-sm font-black text-foreground ">
                      {safeFormatCurrency(data.net_capital_deployed)}
                    </p>
                    <div className="mt-1 flex items-center gap-2">
                       <span className="text-sm text-muted">
                         {data.transaction_count} txns
                       </span>
                    </div>
                  </div>
                );
              }
              return null;
            }}
          />
          <Area
            type="stepAfter"
            dataKey="net_capital_deployed"
            stroke="var(--color-action)"
            strokeWidth={3}
            fillOpacity={1}
            fill="url(#colorDeployed)"
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
      <div className="mt-4 flex items-end justify-between">
        <div>
          <p className="text-sm text-muted">Net capital deployed</p>
          <p className="text-sm font-black text-foreground ">{safeFormatCurrency(points[points.length-1].net_capital_deployed)}</p>
        </div>
        <div className="text-right">
          <p className="text-sm text-muted">Transactions</p>
          <p className="text-sm font-black text-foreground ">{points[points.length-1].transaction_count} txns</p>
        </div>
      </div>
    </div>
  );
}
