const LOCAL_HOSTNAMES = new Set(["127.0.0.1", "localhost", "::1"]);

export type RemoteAccessDecision =
  | { allowed: true; mode: "local" | "private_proxy" }
  | { allowed: false; reason: "remote_disabled" | "identity_missing" | "identity_mismatch" };

function hostnameFromHostHeader(hostHeader: string | null): string {
  const value = (hostHeader ?? "").trim().toLowerCase();
  if (value.startsWith("[")) {
    const closingBracket = value.indexOf("]");
    return closingBracket > 0 ? value.slice(1, closingBracket) : value;
  }
  return value.split(":", 1)[0];
}

export function remoteAccessDecision({
  hostHeader,
  proxyIdentity,
  expectedIdentity,
}: {
  hostHeader: string | null;
  proxyIdentity: string | null;
  expectedIdentity: string | undefined;
}): RemoteAccessDecision {
  const hostname = hostnameFromHostHeader(hostHeader);
  const identity = (proxyIdentity ?? "").trim().toLowerCase();
  const expected = (expectedIdentity ?? "").trim().toLowerCase();
  const localRequest = LOCAL_HOSTNAMES.has(hostname);

  if (localRequest && !identity) {
    return { allowed: true, mode: "local" };
  }
  if (!expected) {
    return { allowed: false, reason: "remote_disabled" };
  }
  if (!identity) {
    return { allowed: false, reason: "identity_missing" };
  }
  if (identity !== expected) {
    return { allowed: false, reason: "identity_mismatch" };
  }
  return { allowed: true, mode: "private_proxy" };
}
