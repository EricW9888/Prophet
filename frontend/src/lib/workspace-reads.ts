"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "./api";

type ReadState = { ready: boolean; error?: string };

/** Independent reads keep their previous data and reject superseded responses. */
export function useWorkspaceReads<Key extends string>() {
  const active = useRef(new Map<Key, AbortController>());
  const [status, setStatus] = useState<Partial<Record<Key, ReadState>>>({});

  useEffect(() => {
    const requests = active.current;
    return () => {
      for (const request of requests.values()) request.abort();
      requests.clear();
    };
  }, []);

  const read = useCallback(async <T>(key: Key, path: string, accept: (data: T) => void, replace = true) => {
    if (!replace && active.current.has(key)) return;
    active.current.get(key)?.abort();
    const request = new AbortController();
    active.current.set(key, request);
    try {
      const data = await apiFetch<T>(path, { signal: request.signal });
      if (request.signal.aborted) return;
      accept(data);
      setStatus((current) => current[key]?.ready && !current[key]?.error
        ? current : { ...current, [key]: { ready: true } });
    } catch (error) {
      if (request.signal.aborted) return;
      const message = error instanceof Error ? error.message : "Request failed.";
      setStatus((current) => ({
        ...current,
        [key]: { ready: current[key]?.ready ?? false, error: message },
      }));
    } finally {
      if (active.current.get(key) === request) active.current.delete(key);
    }
  }, []);

  return { read, status };
}
