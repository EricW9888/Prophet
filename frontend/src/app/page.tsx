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
          description="Your account, how it has moved, and what you hold. Items that need a decision are counted here and handled in Review."
        />
        <DashboardOverview history={<PortfolioHistoryPanel />}>
          <PositionsTable />
          <DeferredDisclosure title="Broker statements and recorded valuation history" className="mt-6 border border-line bg-panel p-4">
            <div className="space-y-4">
              <BrokerEquityPanel />
              <CapturedHoldingsPanel />
            </div>
          </DeferredDisclosure>
        </DashboardOverview>
      </main>
    </div>
  );
}
