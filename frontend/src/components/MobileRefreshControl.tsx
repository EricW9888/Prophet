"use client";

import { RefreshCw } from "lucide-react";
import { useEffect, useRef, useState } from "react";

const REFRESH_THRESHOLD = 72;
const MAX_PULL_DISTANCE = 104;

export default function MobileRefreshControl() {
  const [distance, setDistance] = useState(0);
  const [refreshing, setRefreshing] = useState(false);
  const gesture = useRef({ active: false, startX: 0, startY: 0 });
  const distanceRef = useRef(0);

  useEffect(() => {
    const coarsePointer = window.matchMedia("(pointer: coarse)");

    function updateDistance(nextDistance: number) {
      distanceRef.current = nextDistance;
      setDistance(nextDistance);
    }

    function reset() {
      gesture.current.active = false;
      updateDistance(0);
    }

    function onTouchStart(event: TouchEvent) {
      const target = event.target;
      const excluded =
        target instanceof Element &&
        target.closest(
          "input, textarea, select, button, a, [role='dialog'], [data-pull-refresh-ignore]",
        );
      if (
        !coarsePointer.matches ||
        window.scrollY > 0 ||
        event.touches.length !== 1 ||
        excluded
      ) {
        return;
      }
      const touch = event.touches[0];
      gesture.current = {
        active: true,
        startX: touch.clientX,
        startY: touch.clientY,
      };
    }

    function onTouchMove(event: TouchEvent) {
      if (!gesture.current.active || event.touches.length !== 1) return;
      const touch = event.touches[0];
      const deltaX = touch.clientX - gesture.current.startX;
      const deltaY = touch.clientY - gesture.current.startY;
      if (deltaY <= 0 || Math.abs(deltaX) > deltaY) {
        reset();
        return;
      }
      if (deltaY > 8) event.preventDefault();
      updateDistance(Math.min(MAX_PULL_DISTANCE, Math.sqrt(deltaY) * 9));
    }

    function onTouchEnd() {
      if (!gesture.current.active) return;
      gesture.current.active = false;
      if (distanceRef.current >= REFRESH_THRESHOLD) {
        setRefreshing(true);
        updateDistance(REFRESH_THRESHOLD);
        window.setTimeout(() => window.location.reload(), 120);
        return;
      }
      updateDistance(0);
    }

    window.addEventListener("touchstart", onTouchStart, { passive: true });
    window.addEventListener("touchmove", onTouchMove, { passive: false });
    window.addEventListener("touchend", onTouchEnd, { passive: true });
    window.addEventListener("touchcancel", reset, { passive: true });
    return () => {
      window.removeEventListener("touchstart", onTouchStart);
      window.removeEventListener("touchmove", onTouchMove);
      window.removeEventListener("touchend", onTouchEnd);
      window.removeEventListener("touchcancel", reset);
    };
  }, []);

  const progress = Math.min(1, distance / REFRESH_THRESHOLD);
  return (
    <div
      aria-hidden="true"
      className="pointer-events-none fixed left-1/2 z-[70] flex h-9 w-9 -translate-x-1/2 items-center justify-center rounded-full border border-line bg-panel text-muted shadow-lg transition-opacity duration-150 xl:hidden"
      style={{
        top: "max(0.5rem, env(safe-area-inset-top))",
        opacity: distance > 4 || refreshing ? 1 : 0,
        transform: `translate(-50%, ${Math.max(-48, distance - 52)}px)`,
      }}
    >
      <RefreshCw
        className={`h-4 w-4 ${refreshing ? "animate-spin" : ""}`}
        style={{ transform: refreshing ? undefined : `rotate(${progress * 240}deg)` }}
      />
    </div>
  );
}
