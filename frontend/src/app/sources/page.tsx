"use client";

import AppNav from "@/components/AppNav";
import SourcesWorkspace from "@/components/SourcesWorkspace";

export default function SourcesPage() {
  return (
    <div className="min-h-screen bg-gray-50 text-gray-900 dark:bg-[#0a0a0a] dark:text-gray-100">
      <AppNav active="sources" />
      <SourcesWorkspace />
    </div>
  );
}
