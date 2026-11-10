import AppNav from "@/components/AppNav";
import BrokerEquityPanel from "@/components/BrokerEquityPanel";
import CapturedHoldingsPanel from "@/components/CapturedHoldingsPanel";
import DashboardOverview from "@/components/DashboardOverview";
import PageHeader from "@/components/PageHeader";
import PositionsTable from "@/components/PositionsTable";

export default function Home() {
  return (
    <div className="min-h-screen min-w-0 bg-background text-foreground font-sans">
      <AppNav active="portfolio" />

      <main className="workspace-frame space-y-6 py-6">
        <PageHeader title="Portfolio" description="Review what needs attention, then inspect your holdings and tracked positions." />
        <CapturedHoldingsPanel />
        <BrokerEquityPanel />
        <DashboardOverview>
          <PositionsTable />
        </DashboardOverview>
      </main>
    </div>
  );
}
