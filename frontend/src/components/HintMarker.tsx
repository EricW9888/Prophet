"use client";

import { useId } from "react";

export default function HintMarker({
  label = "Hint",
  children,
}: {
  label?: string;
  children: React.ReactNode;
}) {
  const id = useId();

  return (
    <details className="group relative inline-block">
      <summary
        aria-describedby={id}
        className="flex h-6 w-6 cursor-pointer list-none items-center justify-center rounded-full border border-slate-300 bg-white text-xs font-semibold text-slate-500 transition hover:border-sky-400 hover:text-sky-600 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-400 dark:hover:border-sky-500 dark:hover:text-sky-400"
        title={label}
      >
        ?
      </summary>
      <div
        id={id}
        className="absolute right-0 z-20 mt-2 w-72 rounded-lg border border-slate-200 bg-white p-3 text-sm text-slate-600 shadow-lg dark:border-slate-800 dark:bg-slate-950 dark:text-slate-300"
      >
        {children}
      </div>
    </details>
  );
}
