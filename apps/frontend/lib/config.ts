/**
 * Where the API is, and which principal signs for it.
 *
 * Server-only. None of this is `NEXT_PUBLIC_`, because the credentials that
 * sign requests to an IAM-authorised API must never reach a browser.
 */

/**
 * **The Vercel trap, and why every value comes from one prefix or none.**
 *
 * On Vercel the names `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` are already
 * populated with *Vercel's own* credentials for its build infrastructure. A
 * config that falls back to them signs h2o's requests as somebody else's
 * principal, and the failure is a 403 from API Gateway that looks exactly like a
 * misconfigured policy -- so the debugging goes to IAM and stays there.
 *
 * Reading all three from `H2O_AWS_*` and never falling back means a half-set
 * environment fails as "no credentials" rather than as "the wrong ones".
 */
const REQUIRED = ["H2O_AWS_ACCESS_KEY_ID", "H2O_AWS_SECRET_ACCESS_KEY"] as const;

function read(name: string): string | undefined {
  const value = process.env[name];
  // `.trim()`, because a secret key pasted into a dashboard field arrives with
  // a trailing newline surprisingly often, and the only symptom is
  // SignatureDoesNotMatch -- which reads as a wrong key, not a whitespace one.
  return value?.trim() || undefined;
}

export const region = read("H2O_AWS_REGION") ?? "eu-west-1";
export const apiUrl = (read("H2O_API_URL") ?? "").replace(/\/$/, "");

export function credentials() {
  const accessKeyId = read("H2O_AWS_ACCESS_KEY_ID");
  const secretAccessKey = read("H2O_AWS_SECRET_ACCESS_KEY");
  const sessionToken = read("H2O_AWS_SESSION_TOKEN");

  if (!accessKeyId || !secretAccessKey) {
    const missing = REQUIRED.filter((name) => !read(name));
    throw new Error(
      `missing ${missing.join(" and ")}. These are deliberately not the AWS_* names: ` +
        `on Vercel those already hold Vercel's own credentials, and falling back to ` +
        `them signs as the wrong principal.`,
    );
  }
  return { accessKeyId, secretAccessKey, sessionToken };
}

export function assertConfigured() {
  if (!apiUrl) throw new Error("missing H2O_API_URL — the deployed API's base URL");
  credentials();
}
