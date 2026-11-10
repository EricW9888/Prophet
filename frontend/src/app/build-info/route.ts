export const dynamic = "force-dynamic";

export function GET() {
  const build = process.env.PROPHET_COMPILED_BUILD ?? "";
  const instance = process.env.PROPHET_LAUNCH_INSTANCE ?? "";
  return Response.json({
    build: /^[0-9a-f]{64}$/.test(build) ? build : null,
    instance: /^[0-9a-f]{32}$/.test(instance) ? instance : null,
  }, { headers: { "Cache-Control": "no-store" } });
}
