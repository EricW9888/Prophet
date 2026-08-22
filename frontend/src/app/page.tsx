import AppNav from "@/components/AppNav";
import DashboardOverview from "@/components/DashboardOverview";
import PositionsTable from "@/components/PositionsTable";

export default function Home() {
  return (
    <div className="min-h-screen bg-gray-50 dark:bg-[#0a0a0a] text-gray-900 dark:text-gray-100 font-sans selection:bg-indigo-500/30">
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
