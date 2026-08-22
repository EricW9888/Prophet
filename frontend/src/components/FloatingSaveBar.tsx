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
    <div className={`fixed bottom-8 left-1/2 -translate-x-1/2 z-[100] transition-all duration-300 transform ${isDirty ? "translate-y-0 opacity-100" : "translate-y-12 opacity-0 pointer-events-none"}`}>
      <div className="flex items-center gap-4 rounded-2xl border border-indigo-100 bg-white/90 px-4 py-3 shadow-2xl backdrop-blur-xl dark:border-indigo-900/50 dark:bg-gray-950/90">
        <div className="flex items-center gap-3 pr-4 border-r border-gray-100 dark:border-gray-800">
          <div className="h-2 w-2 animate-pulse rounded-full bg-indigo-500" />
          <p className="text-sm font-medium text-gray-700 dark:text-gray-200">
            Unsaved changes
          </p>
        </div>

        <div className="flex items-center gap-2">
          <button
            onClick={onDiscard}
            disabled={saving}
            className="flex items-center gap-2 rounded-xl px-3 py-2 text-sm font-medium text-gray-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-900"
          >
            <RotateCcw className="h-4 w-4" />
            Discard
          </button>
          <button
            onClick={onSave}
            disabled={saving}
            className="flex items-center gap-2 rounded-xl bg-indigo-600 px-5 py-2 text-sm font-bold text-white shadow-lg shadow-indigo-500/20 hover:bg-indigo-500 active:scale-95 transition-all disabled:opacity-50"
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
