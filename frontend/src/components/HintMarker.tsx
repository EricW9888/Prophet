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
        className="flex h-6 w-6 cursor-pointer list-none items-center justify-center rounded-full border border-gray-300 bg-white text-xs font-semibold text-gray-500 transition hover:border-indigo-400 hover:text-indigo-600 dark:border-gray-700 dark:bg-gray-950 dark:text-gray-400 dark:hover:border-indigo-500 dark:hover:text-indigo-400"
        title={label}
      >
        ?
      </summary>
      <div
        id={id}
        className="absolute right-0 z-20 mt-2 w-72 rounded-xl border border-gray-200 bg-white p-3 text-sm text-gray-600 shadow-lg dark:border-gray-800 dark:bg-gray-950 dark:text-gray-300"
      >
        {children}
      </div>
    </details>
  );
}
