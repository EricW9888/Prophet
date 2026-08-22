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
  const scrollRef = useRef<HTMLDivElement>(null);
  const eventSourceRef = useRef<EventSource | null>(null);

  useEffect(() => {
    const es = new EventSource(`${API_BASE}/integrations/gmail/backfill/logs`);
    eventSourceRef.current = es;

    es.onmessage = (event) => {
      setLogs((prev) => [...prev.slice(-100), event.data]);
    };

    es.onerror = () => {
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

  return (
    <div className={`fixed bottom-8 right-8 z-[100] bg-black border border-gray-800 rounded-2xl shadow-2xl overflow-hidden transition-all duration-300 flex flex-col ${
      isMaximized ? "inset-8" : "w-[500px] h-[400px]"
    }`}>
      {/* Header */}
      <div className="flex items-center justify-between px-6 py-4 bg-gray-900/50 border-b border-gray-800">
        <div className="flex items-center gap-3">
          <Terminal className="w-4 h-4 text-emerald-500" />
          <span className="text-xs font-bold uppercase tracking-widest text-gray-400">Backfill Monitor</span>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setLogs([])}
            className="p-1.5 hover:bg-gray-800 rounded-lg transition-colors"
            title="Clear Console"
          >
            <Trash2 className="w-4 h-4 text-gray-500" />
          </button>
          <button
            onClick={() => setIsMaximized(!isMaximized)}
            className="p-1.5 hover:bg-gray-800 rounded-lg transition-colors"
          >
            {isMaximized ? <Minimize2 className="w-4 h-4 text-gray-500" /> : <Maximize2 className="w-4 h-4 text-gray-500" />}
          </button>
          <button
            onClick={onClose}
            className="p-1.5 hover:bg-gray-800 rounded-lg transition-colors"
          >
            <X className="w-4 h-4 text-gray-500" />
          </button>
        </div>
      </div>

      {/* Logs */}
      <div
        ref={scrollRef}
        className="flex-1 p-6 overflow-y-auto font-mono text-sm space-y-1.5 bg-black scrollbar-thin scrollbar-thumb-gray-800"
      >
        {logs.length === 0 && (
          <p className="text-gray-600 animate-pulse">Waiting for logs...</p>
        )}
        {logs.map((log, i) => {
          let colorClass = "text-gray-400";
          if (log.includes("SUCCESS")) colorClass = "text-emerald-400 font-bold";
          if (log.includes("FAILED") || log.includes("Error")) colorClass = "text-red-400";
          if (log.includes("Scanning")) colorClass = "text-blue-400";
          if (log.includes("IRRELEVANT")) colorClass = "text-amber-500/70";

          return (
            <div key={i} className={`whitespace-pre-wrap ${colorClass}`}>
              <span className="text-gray-600 mr-2 opacity-50">[{i+1}]</span>
              {log}
            </div>
          );
        })}
      </div>

      {/* Footer / Status */}
      <div className="px-6 py-3 bg-gray-900/30 border-t border-gray-800 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <div className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse" />
          <span className="text-[10px] font-bold text-gray-500 uppercase tracking-widest">Live Stream Active</span>
        </div>
        <span className="text-[10px] font-mono text-gray-600 italic">Ollama-accelerated</span>
      </div>
    </div>
  );
}
