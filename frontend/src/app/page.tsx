import AppNav from "@/components/AppNav";
import BrokerEquityPanel from "@/components/BrokerEquityPanel";
import CapturedHoldingsPanel from "@/components/CapturedHoldingsPanel";
import PortfolioHistoryPanel from "@/components/PortfolioHistoryPanel";
import DashboardOverview from "@/components/DashboardOverview";
import PageHeader from "@/components/PageHeader";
import PositionsTable from "@/components/PositionsTable";
import DeferredDisclosure from "@/components/DeferredDisclosure";

export default function Home() {
  return (
    <div className="min-h-screen min-w-0 bg-background text-foreground font-sans">
      <AppNav active="portfolio" />

      <main className="workspace-frame space-y-6 py-6">
        <PageHeader
          title="Portfolio"
          description="Review what needs attention, then inspect your holdings and tracked positions."
          actions={<nav aria-label="Portfolio sections" className="flex flex-wrap gap-2">
            <a href="#investment-attention" className="workspace-control">Attention</a>
            <a href="#portfolio-history" className="workspace-control">History</a>
            <a href="#portfolio-holdings" className="workspace-control">Holdings</a>
          </nav>}
        />
        <DashboardOverview history={<>
          <BrokerEquityPanel />
          <PortfolioHistoryPanel />
          <DeferredDisclosure title="Recorded valuation history" className="border border-line bg-panel p-4">
            <CapturedHoldingsPanel />
          </DeferredDisclosure>
        </>}>
          <PositionsTable />
        </DashboardOverview>
      </main>
    </div>
  );
}
