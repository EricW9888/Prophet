"use client";

import { useEffect, useRef, useState } from "react";
import { Terminal, X, Maximize2, Minimize2, Trash2 } from "lucide-react";
import { API_BASE } from "@/lib/api";

interface LiveLogConsoleProps {
  onClose: () => void;
}

export default function LiveLogConsole({ onClose }: LiveLogConsoleProps) {
  const [logs, setLogs] = useState<string[]>([]);
  const [isMaximized, setIsMaximized] = useState(false);
  const [connectionState, setConnectionState] = useState<"connecting" | "live" | "reconnecting">("connecting");
  const scrollRef = useRef<HTMLDivElement>(null);
  const eventSourceRef = useRef<EventSource | null>(null);

  useEffect(() => {
    const es = new EventSource(`${API_BASE}/integrations/gmail/backfill/logs`);
    eventSourceRef.current = es;

    es.onopen = () => {
      setConnectionState("live");
    };

    es.onmessage = (event) => {
      setConnectionState("live");
      setLogs((prev) => [...prev.slice(-100), event.data]);
    };

    es.onerror = () => {
      setConnectionState("reconnecting");
      setLogs((prev) => [...prev, "[SYSTEM] Connection to log stream lost. Retrying..."]);
    };

    return () => {
      es.close();
    };
  }, []);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [logs]);

  const statusLabel = connectionState === "live"
    ? "Live stream active"
    : connectionState === "reconnecting"
      ? "Reconnecting"
      : "Connecting";

  return (
    <div className={`fixed z-[100] flex flex-col overflow-hidden rounded-lg border border-slate-800 bg-black shadow-lg transition-all duration-300 ${
      isMaximized
        ? "inset-3 sm:inset-8"
        : "inset-x-4 bottom-4 h-[min(400px,calc(100dvh-2rem))] sm:inset-x-auto sm:bottom-8 sm:right-8 sm:h-[400px] sm:w-[500px]"
    }`}>
      {/* Header */}
      <div className="flex items-center justify-between gap-3 border-b border-slate-800 bg-slate-900/50 px-4 py-3 sm:px-6 sm:py-4">
        <div className="flex items-center gap-3">
          <Terminal className="w-4 h-4 text-emerald-500" />
          <span className="text-xs font-bold uppercase tracking-widest text-slate-400">Backfill Monitor</span>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setLogs([])}
            className="p-1.5 hover:bg-slate-800 rounded-lg transition-colors"
            title="Clear Console"
            aria-label="Clear console"
          >
            <Trash2 className="w-4 h-4 text-slate-500" />
          </button>
          <button
            onClick={() => setIsMaximized(!isMaximized)}
            className="p-1.5 hover:bg-slate-800 rounded-lg transition-colors"
            aria-label={isMaximized ? "Restore log console" : "Maximize log console"}
          >
            {isMaximized ? <Minimize2 className="w-4 h-4 text-slate-500" /> : <Maximize2 className="w-4 h-4 text-slate-500" />}
          </button>
          <button
            onClick={onClose}
            className="p-1.5 hover:bg-slate-800 rounded-lg transition-colors"
            aria-label="Close log console"
          >
            <X className="w-4 h-4 text-slate-500" />
          </button>
        </div>
      </div>

      {/* Logs */}
      <div
        ref={scrollRef}
        className="flex-1 space-y-1.5 overflow-y-auto bg-black p-4 font-mono text-xs scrollbar-thin scrollbar-thumb-slate-800 sm:p-6 sm:text-sm"
      >
        {logs.length === 0 && (
          <p className="text-slate-600 animate-pulse">Waiting for logs...</p>
        )}
        {logs.map((log, i) => {
          let colorClass = "text-slate-400";
          if (log.includes("SUCCESS")) colorClass = "text-emerald-400 font-bold";
          if (log.includes("FAILED") || log.includes("Error")) colorClass = "text-red-400";
          if (log.includes("Scanning")) colorClass = "text-blue-400";
          if (log.includes("IRRELEVANT")) colorClass = "text-amber-500/70";

          return (
            <div key={i} className={`whitespace-pre-wrap ${colorClass}`}>
              <span className="text-slate-600 mr-2 opacity-50">[{i+1}]</span>
              {log}
            </div>
          );
        })}
      </div>

      {/* Footer / Status */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-slate-800 bg-slate-900/30 px-4 py-3 sm:px-6">
        <div className="flex items-center gap-2">
          <div className={`h-2 w-2 rounded-full ${
            connectionState === "live" ? "animate-pulse bg-emerald-500" : "bg-amber-500"
          }`} />
          <span className="text-[10px] font-bold uppercase tracking-widest text-slate-500">{statusLabel}</span>
        </div>
        <span className="text-[10px] font-mono text-slate-600">Gmail backfill</span>
      </div>
    </div>
  );
}
