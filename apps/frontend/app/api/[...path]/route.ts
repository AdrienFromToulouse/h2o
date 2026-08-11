import { NextRequest } from "next/server";

import { call } from "@/lib/api-proxy";

/**
 * The browser's only way to the API.
 *
 * Client components (the review card, the polling hook) need to POST and to
 * re-read while the page is live, and they cannot sign. This route forwards
 * whatever they ask for, signed, and returns the answer verbatim -- including
 * the status, so a 422 from the integrity gate stays a 422 and the card can
 * render the findings rather than a generic failure.
 */
export const dynamic = "force-dynamic";

async function forward(request: NextRequest, path: string[]) {
  // Each segment is encoded on the way out. A gap id is a surface form, so
  // "gas bottle" is a real path segment with a real space in it.
  const target = "/" + path.map(encodeURIComponent).join("/");
  const body = request.method === "GET" ? undefined : await request.text();

  const response = await call(target, {
    method: request.method,
    body: body || undefined,
    search: request.nextUrl.searchParams,
  });

  return new Response(await response.text(), {
    status: response.status,
    headers: { "content-type": "application/json" },
  });
}

export async function GET(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  return forward(request, (await context.params).path);
}

export async function POST(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  return forward(request, (await context.params).path);
}
