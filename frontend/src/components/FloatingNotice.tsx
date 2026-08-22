"use client";

type FloatingNoticeProps = {
  tone: "success" | "error";
  message: string;
  onDismiss?: () => void;
};

export default function FloatingNotice({
  tone,
  message,
  onDismiss,
}: FloatingNoticeProps) {
  const toneClass =
    tone === "success"
      ? "border-emerald-200 bg-emerald-50 text-emerald-800 dark:border-emerald-900 dark:bg-emerald-950/90 dark:text-emerald-200"
      : "border-red-200 bg-red-50 text-red-800 dark:border-red-900 dark:bg-red-950/90 dark:text-red-200";

  return (
    <div className="fixed inset-x-4 top-20 z-50 flex justify-center pointer-events-none">
      <div
        className={`pointer-events-auto flex w-full max-w-2xl items-start justify-between gap-4 rounded-2xl border px-4 py-3 shadow-xl backdrop-blur ${toneClass}`}
        role="status"
        aria-live="polite"
      >
        <p className="text-sm font-medium">{message}</p>
        {onDismiss ? (
          <button
            type="button"
            onClick={onDismiss}
            className="shrink-0 rounded-full border border-current/20 px-2 py-1 text-xs font-semibold uppercase tracking-wider"
          >
            Dismiss
          </button>
        ) : null}
      </div>
    </div>
  );
}
