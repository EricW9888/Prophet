import { Check, Circle, Clock3, LoaderCircle, Pause, Square, TriangleAlert, WifiOff } from "lucide-react";
import type { ActivityState } from "@/lib/work-status";

export default function ActivityIndicator({ state, className = "" }: { state: ActivityState; className?: string }) {
  const Icon = { working: LoaderCircle, queued: Clock3, waiting: Pause, reconnecting: WifiOff, idle: Circle, error: TriangleAlert, completed: Check, cancelled: Square }[state];
  return <Icon aria-hidden="true" className={`h-4 w-4 shrink-0 ${state === "working" ? "motion-safe:animate-spin text-action" : state === "waiting" || state === "error" || state === "reconnecting" ? "text-caution" : state === "completed" ? "text-positive" : "text-muted"} ${className}`} />;
}
