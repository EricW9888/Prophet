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
  if (points.length === 0) return <div className="py-12 text-center text-slate-400 italic text-sm">No series data</div>;


  return (
    <div className="mt-4 min-w-0">
      <ResponsiveContainer width="100%" height={180}>
        <AreaChart data={points}>
          <defs>
            <linearGradient id="colorDeployed" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor="#0284c7" stopOpacity={0.3}/>
              <stop offset="95%" stopColor="#0284c7" stopOpacity={0}/>
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="rgba(148,163,184,0.18)" />
          <XAxis
            dataKey="as_of"
            hide
          />
          <YAxis
            hide
            domain={['dataMin - 5000', 'dataMax + 5000']}
          />
          <Tooltip
            content={({ active, payload }) => {
              if (active && payload && payload.length) {
                const data = payload[0].payload;
                return (
                  <div className="rounded-lg border border-slate-200 bg-white/95 p-3 shadow-xl backdrop-blur-sm dark:border-slate-700 dark:bg-slate-950/95">
                    <p className="mb-1 text-[10px] font-bold uppercase tracking-widest text-slate-400 dark:text-slate-500">
                      {new Date(data.as_of).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })}
                    </p>
                    <p className="text-sm font-black text-slate-900 dark:text-slate-100">
                      {safeFormatCurrency(data.net_capital_deployed)}
                    </p>
                    <div className="mt-1 flex items-center gap-2">
                       <span className="rounded bg-sky-50 px-1.5 py-0.5 text-[10px] font-bold text-sky-500 dark:bg-sky-950/30 dark:text-sky-300">
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
            type="monotone"
            dataKey="net_capital_deployed"
            stroke="#0284c7"
            strokeWidth={3}
            fillOpacity={1}
            fill="url(#colorDeployed)"
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
      <div className="mt-4 flex items-end justify-between">
        <div>
          <p className="text-[10px] font-bold uppercase tracking-tighter text-slate-400 dark:text-slate-500">Current Inflow</p>
          <p className="text-sm font-black text-slate-900 dark:text-slate-100">{safeFormatCurrency(points[points.length-1].net_capital_deployed)}</p>
        </div>
        <div className="text-right">
          <p className="text-[10px] font-bold uppercase tracking-tighter text-slate-400 dark:text-slate-500">Velocity</p>
          <p className="text-sm font-black text-slate-900 dark:text-slate-100">{points[points.length-1].transaction_count} txns</p>
        </div>
      </div>
    </div>
  );
}
