import AppNav from "@/components/AppNav";
import { WorkspaceSkeleton } from "@/components/WorkspaceState";

export default function Loading() {
  return <><AppNav /><main className="workspace-frame py-6"><WorkspaceSkeleton label="Opening workspace" rows={4} /></main></>;
}
