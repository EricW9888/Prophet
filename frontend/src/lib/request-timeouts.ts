// Keep the proxy alive beyond every supported client request budget. These are
// request deadlines, not retries or permission to run unbounded background work.
export const API_TIMEOUT_MS = 15_000;
// The backend bounds model translation generation to 45 seconds.
export const TRANSLATION_REQUEST_TIMEOUT_MS = 50_000;
export const DISCOVERY_RUN_TIMEOUT_MS = 120_000;
export const API_PROXY_TIMEOUT_MS = Math.max(
  API_TIMEOUT_MS,
  TRANSLATION_REQUEST_TIMEOUT_MS,
  DISCOVERY_RUN_TIMEOUT_MS,
) + 5_000;
