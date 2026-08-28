"use client";

import { Save, RotateCcw } from "lucide-react";

interface FloatingSaveBarProps {
  isDirty: boolean;
  onSave: () => void;
  onDiscard: () => void;
  saving?: boolean;
}

export default function FloatingSaveBar({ isDirty, onSave, onDiscard, saving }: FloatingSaveBarProps) {
  if (!isDirty) return null;

  return (
    <div className={`fixed bottom-4 left-1/2 z-[100] w-[calc(100vw-2rem)] max-w-xl -translate-x-1/2 transform transition-all duration-300 sm:bottom-8 ${isDirty ? "translate-y-0 opacity-100" : "translate-y-12 opacity-0 pointer-events-none"}`}>
      <div className="flex flex-col gap-3 rounded-lg border border-sky-100 bg-white/95 px-4 py-3 shadow-lg backdrop-blur-xl sm:flex-row sm:items-center sm:gap-4 dark:border-sky-900/50 dark:bg-slate-950/95">
        <div className="flex items-center gap-3 sm:border-r sm:border-slate-100 sm:pr-4 dark:sm:border-slate-800">
          <div className="h-2 w-2 animate-pulse rounded-full bg-sky-500" />
          <p className="text-sm font-medium text-slate-700 dark:text-slate-200">
            Unsaved changes
          </p>
        </div>

        <div className="flex items-center justify-end gap-2">
          <button
            onClick={onDiscard}
            disabled={saving}
            className="flex items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium text-slate-500 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-900"
          >
            <RotateCcw className="h-4 w-4" />
            Discard
          </button>
          <button
            onClick={onSave}
            disabled={saving}
            className="flex items-center gap-2 rounded-lg bg-sky-600 px-5 py-2 text-sm font-bold text-white shadow-lg shadow-sky-500/20 hover:bg-sky-500 active:scale-95 transition-all disabled:opacity-50"
          >
            {saving ? (
              <div className="h-4 w-4 animate-spin rounded-full border-2 border-white border-t-transparent" />
            ) : (
              <Save className="h-4 w-4" />
            )}
            {saving ? "Saving..." : "Save Changes"}
          </button>
        </div>
      </div>
    </div>
  );
}
