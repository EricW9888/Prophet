"use client";

import { Monitor } from "lucide-react";
import { useProphetInstall } from "@/components/PwaRegistration";

export default function DesktopAppSettings() {
  const { standalone, canInstall, state, install } = useProphetInstall();
  const installed = state === "accepted" || state === "installed";
  return (
    <section id="desktop-window" className="mb-5 flex scroll-mt-20 flex-wrap items-start justify-between gap-3 border border-line bg-panel p-4">
      <div className="flex max-w-2xl items-start gap-3">
        <Monitor size={18} className="mt-0.5 shrink-0 text-muted" aria-hidden="true" />
        <div>
          <h2 className="text-sm font-semibold">Desktop window</h2>
          <p className="mt-1 text-sm text-muted" role="status">
            {standalone ? "Prophet is open in its own window."
              : installed ? "Open Prophet from your Start menu or your browser’s apps list once installation finishes."
                : "Open Prophet in its own window without a browser tab bar."}
          </p>
          {!standalone && !installed && !canInstall ? (
            <p className="mt-1 text-xs text-muted">{state === "failed" ? "The install prompt didn’t open. " : ""}Open this address in Edge or Chrome, then choose Install app from the browser menu.</p>
          ) : null}
          <p className="mt-1 text-xs text-muted">Keep the local Prophet launcher running to use your portfolio and research.</p>
        </div>
      </div>
      {!standalone && !installed && canInstall ? <button type="button" className="workspace-control" onClick={() => void install()}>Install Prophet</button> : null}
    </section>
  );
}
