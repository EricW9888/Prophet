"use client";

import AppNav from "@/components/AppNav";
import SourcesWorkspace from "@/components/SourcesWorkspace";

export default function SourcesPage() {
  return (
    <div className="min-h-screen bg-background text-foreground">
      <AppNav active="sources" />
      <SourcesWorkspace />
    </div>
  );
}
