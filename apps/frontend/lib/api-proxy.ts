import { AwsClient } from "aws4fetch";

import { apiUrl, credentials, region } from "./config";

/**
 * The signing proxy. Every browser request to the API goes through here.
 *
 * The API is IAM-authorised (ADR-007), so a request needs a SigV4 signature and
 * therefore a credential — which means it cannot be made from a browser. The
 * console's server routes sign on the user's behalf and the credential never
 * leaves the server.
 *
 * **Path escaping.** aws4fetch signs the URL as given. API Gateway paths here
 * carry nothing that needs escaping, but a gap id is a surface form and really
 * does contain spaces ("gas bottle"), so the caller encodes segments and the
 * encoded form is what gets signed. When AgentCore lands in M5 this matters
 * more: its path carries a percent-encoded runtime ARN that must be encoded
 * *twice* to sign correctly, and getting it wrong produces
 * SignatureDoesNotMatch on chat while every other route keeps working — so the
 * bug hides behind a working API.
 */
export async function call(
  path: string,
  init: { method?: string; body?: string; search?: URLSearchParams } = {},
): Promise<Response> {
  const client = new AwsClient({ ...credentials(), region, service: "execute-api" });

  const query = init.search?.toString();
  const url = `${apiUrl}${path}${query ? `?${query}` : ""}`;
  const method = init.method ?? "GET";

  const response = await client.fetch(url, {
    method,
    body: init.body,
    headers: init.body ? { "content-type": "application/json" } : undefined,
  });

  if (!response.ok) {
    // **Log the request we made, not the upstream body.** API Gateway answers
    // every failure with {"message":"Internal server error"}, so echoing that
    // to the console tells you nothing at all — where the method, the path and
    // the status tell you which call failed and how.
    console.error(`[api] ${method} ${path} -> ${response.status}`);
  }
  return response;
}

/** A JSON GET, for server components that render from the API. */
export async function get<T>(path: string, search?: URLSearchParams): Promise<T> {
  const response = await call(path, { search });
  if (!response.ok) {
    throw new Error(`GET ${path} failed with ${response.status}`);
  }
  return (await response.json()) as T;
}
