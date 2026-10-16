"use client";

import { useParams } from "next/navigation";
import EvidenceReceipt from "@/components/EvidenceReceipt";
import AppNav from "@/components/AppNav";

export default function EvidenceReceiptPage() {
  const params = useParams<{ id: string }>();
  const evidenceId = Array.isArray(params.id) ? params.id[0] : params.id;
  return <main className="min-h-screen bg-background text-foreground">
    <AppNav active="sources" />
    <EvidenceReceipt evidenceId={evidenceId} />
  </main>;
}
