import AppNav from "@/components/AppNav";
import DashboardOverview from "@/components/DashboardOverview";
import PositionsTable from "@/components/PositionsTable";

export default function Home() {
  return (
    <div className="min-h-screen bg-background text-foreground font-sans">
      <AppNav active="portfolio" />

      <main className="mx-auto w-full max-w-[1440px] space-y-6 px-4 py-6 sm:px-6 lg:px-8">
        <DashboardOverview />

        <section>
          <PositionsTable />
        </section>
      </main>
    </div>
  );
}
