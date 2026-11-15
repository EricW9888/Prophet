import Link from "next/link";
import AppNav from "@/components/AppNav";
import PageHeader from "@/components/PageHeader";
import PatternReviews from "@/components/PatternReviews";

export default function PatternReviewsPage() {
  return <div className="min-h-screen min-w-0 overflow-x-clip bg-background text-foreground">
    <AppNav active="opportunities" />
    <main className="workspace-frame py-7">
      <PageHeader eyebrow="Discovery" title="Pattern reviews" description="Follow proposed connections from their captured evidence to a testable investigation." actions={<Link href="/opportunities" className="border border-line px-3 py-2 text-sm">Opportunity queue</Link>} />
      <PatternReviews />
    </main>
  </div>;
}
