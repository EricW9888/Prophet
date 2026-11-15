import WorkspaceState from "@/components/WorkspaceState";

export default function Loading() {
  return <main className="workspace-frame py-6"><WorkspaceState kind="loading" title="Loading workspace" description="Opening the requested view." /></main>;
}
