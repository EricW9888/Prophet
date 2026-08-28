import { NextRequest, NextResponse } from "next/server";

import { remoteAccessDecision } from "@/lib/remoteAccess";

export function proxy(request: NextRequest) {
  const decision = remoteAccessDecision({
    hostHeader: request.headers.get("host"),
    proxyIdentity: request.headers.get("tailscale-user-login"),
    expectedIdentity: process.env.PROPHET_REMOTE_ACCESS_USER,
  });

  if (decision.allowed) {
    return NextResponse.next();
  }

  return NextResponse.json(
    {
      detail:
        "Prophet private access is disabled or this authenticated network identity is not authorized.",
    },
    {
      status: 403,
      headers: { "Cache-Control": "no-store" },
    },
  );
}

export const config = {
  matcher: ["/((?!_next/static|_next/image).*)"],
};
