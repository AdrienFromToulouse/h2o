import { get } from "@/lib/api-proxy";
import type { Run } from "@/lib/types";

export const dynamic = "force-dynamic";

function when(value?: string) {
  return value ? new Date(value).toLocaleString() : "—";
}

export default async function RunsPage() {
  const runs = await get<Run[]>("/runs");

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Runs</h1>
        <p className="mt-2 text-muted">
          Publishing is the beginning of the work, not the end of it. This is what happened
          afterwards.
        </p>
      </div>

      {runs.length === 0 ? (
        <p className="rounded-lg border border-line bg-white p-5 text-muted">
          Nothing has run yet.
        </p>
      ) : (
        <ul className="space-y-3">
          {runs.map((run) => (
            <li key={run.run_id} className="rounded-lg border border-line bg-white p-5">
              <div className="flex items-baseline justify-between gap-4">
                <span className="font-medium">
                  {run.kind === "ingest" ? "Ingestion" : "Change published"}
                  {run.concept_id ? ` · ${run.concept_id}` : ""}
                </span>
                <span
                  className={
                    run.status === "failed" ? "text-sm text-red-700" : "text-sm text-muted"
                  }
                >
                  {run.status}
                </span>
              </div>
              <p className="mt-1 text-sm text-muted">{when(run.started_at)}</p>
              {run.summary ? <p className="mt-2 text-sm">{run.summary}</p> : null}
              {run.error ? <p className="mt-2 text-sm text-red-700">{run.error}</p> : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
