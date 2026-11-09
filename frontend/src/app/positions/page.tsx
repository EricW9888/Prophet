import Link from "next/link";
import AppNav from "@/components/AppNav";
import PageHeader from "@/components/PageHeader";
import PositionsTable from "@/components/PositionsTable";
import ReconcilePanel from "@/components/ReconcilePanel";

export default function PositionsPage() {
  return (
    <div className="min-h-screen bg-background text-foreground font-sans">
      <AppNav active="portfolio" />

      <main className="workspace-frame space-y-6 py-6">
        <PageHeader
          title="Positions"
          description="Inspect holdings and tracked names, record transactions, and reconcile the ledger."
          actions={<Link href="/verification?domain=portfolio_data" className="workspace-control">Portfolio data review</Link>}
        />

        <section>
          <PositionsTable />
        </section>

        <ReconcilePanel />
      </main>
    </div>
  );
}
