"use client";

import { useEffect, useState } from "react";

import { LABELS } from "@/lib/labels";
import type { ConceptDetail, Impact } from "@/lib/types";

/**
 * The review card: everything a curator needs to judge one change.
 *
 * **The technical toggle's state is deliberately not persisted, and that is a
 * mechanic rather than a convention.** In the App Router, navigating from
 * /vocabulary/carbon-filter to /vocabulary/co2-cylinder reuses this component
 * rather than remounting it, so a `useState(false)` here would survive the
 * navigation and the toggle would quietly become the sticky "technical mode"
 * ADR-006 §2 forbids. The page renders `<ReviewCard key={conceptId}>`, which
 * forces a remount, and tests/review-card.test.tsx opens the toggle on one
 * concept and asserts it is closed on the next.
 */
export function ReviewCard({ concept, prefill }: { concept: ConceptDetail; prefill?: string }) {
  const [altLabels, setAltLabels] = useState<string[]>(concept.alt_labels ?? []);
  const [pending, setPending] = useState(prefill ?? "");
  const [changeNote, setChangeNote] = useState("");
  const [impact, setImpact] = useState<Impact | null>(null);
  const [technical, setTechnical] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [findings, setFindings] = useState<{ message: string }[]>([]);
  const [runId, setRunId] = useState<string | null>(null);

  const proposed = pending.trim() && !altLabels.includes(pending.trim())
    ? [...altLabels, pending.trim()]
    : altLabels;
  const changed = proposed.length !== (concept.alt_labels ?? []).length;

  const draft = {
    concept_id: concept.concept_id,
    pref_label: concept.pref_label.en,
    alt_labels: proposed,
    definition: concept.definition?.en,
    scheme_id: concept.scheme_id ?? undefined,
    broader: concept.parent?.concept_id,
    change_note: changeNote || undefined,
  };

  // Debounced, so the sentence updates as the expert types. ADR-005 §4 puts
  // this *before* Save on purpose: it converts an abstract edit into a visible
  // consequence, which is what makes the console more than a form.
  useEffect(() => {
    if (!changed) {
      setImpact(null);
      return;
    }
    const timer = setTimeout(async () => {
      const response = await fetch(`/api/vocabulary/concepts/${concept.concept_id}/impact`, {
        method: "POST",
        body: JSON.stringify(draft),
      });
      if (response.ok) setImpact((await response.json()) as Impact);
    }, 400);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pending, changed, concept.concept_id]);

  async function publish() {
    setPublishing(true);
    setFindings([]);
    const response = await fetch(`/api/vocabulary/concepts/${concept.concept_id}/publish`, {
      method: "POST",
      body: JSON.stringify({ draft, author: "console" }),
    });
    const body = await response.json();
    setPublishing(false);

    if (response.status === 422) {
      // Rendered verbatim. ADR-006: never a code, never the query, never SKOS.
      setFindings(body?.detail?.findings ?? [{ message: String(body?.detail ?? "") }]);
      return;
    }
    if (!response.ok) {
      setFindings([{ message: body?.detail ?? "That did not save. Try again." }]);
      return;
    }
    setRunId(body.run_id);
  }

  return (
    <article className="space-y-6 rounded-lg border border-line bg-white p-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">{concept.pref_label.en}</h1>
        <p className="mt-1 text-sm text-muted">
          {LABELS.versionInfo} {concept.version}
          {concept.parent ? ` · ${LABELS.broader} ${concept.parent.pref_label}` : ""}
        </p>
      </header>

      {concept.definition?.en ? (
        <section>
          <h2 className="text-sm font-medium text-muted">{LABELS.definition}</h2>
          <p className="mt-1">{concept.definition.en}</p>
        </section>
      ) : null}

      <section>
        <h2 className="text-sm font-medium text-muted">{LABELS.altLabel}</h2>
        <ul className="mt-2 flex flex-wrap gap-2">
          {altLabels.map((label) => (
            <li key={label} className="rounded border border-line px-2 py-1 text-sm">
              {label}
            </li>
          ))}
          {pending.trim() && !altLabels.includes(pending.trim()) ? (
            <li className="rounded border border-dashed border-accent px-2 py-1 text-sm text-accent">
              {pending.trim()}
            </li>
          ) : null}
        </ul>
        <div className="mt-3 flex gap-2">
          <input
            aria-label="Add another term people use"
            placeholder="Add another term people use"
            value={pending}
            onChange={(event) => setPending(event.target.value)}
            className="w-full rounded border border-line px-3 py-2 text-sm"
          />
          <button
            type="button"
            onClick={() => {
              if (pending.trim()) setAltLabels([...altLabels, pending.trim()]);
              setPending("");
            }}
            className="rounded border border-line px-3 py-2 text-sm"
          >
            Add
          </button>
        </div>
      </section>

      {concept.machine_signals?.length ? (
        <section>
          <h2 className="text-sm font-medium text-muted">Machine signals</h2>
          {/* Read-only, and instrument *names* only. ADR-006 names this as one
              of exactly two places a signal name may appear outside the toggle. */}
          <ul className="mt-1 space-y-1 text-sm">
            {concept.machine_signals.map((signal) => (
              <li key={signal.signal} className="text-muted">
                {signal.signal}
                {signal.unit ? ` (${signal.unit})` : ""}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {changed ? (
        <section className="space-y-3 rounded border border-line bg-slate-50 p-4">
          <label className="block text-sm font-medium text-muted" htmlFor="change-note">
            {LABELS.changeNote}
          </label>
          <input
            id="change-note"
            value={changeNote}
            onChange={(event) => setChangeNote(event.target.value)}
            placeholder="Why is this the right term?"
            className="w-full rounded border border-line px-3 py-2 text-sm"
          />

          {/* Directly above Save, which is where ADR-006 puts it. */}
          {impact?.sentence ? (
            <p className="text-sm font-medium">{impact.sentence}</p>
          ) : (
            <p className="text-sm text-muted">
              {impact ? "This change resolves no waiting mentions." : "Working out what this does…"}
            </p>
          )}

          {findings.length > 0 ? (
            <ul className="space-y-1 rounded border border-red-200 bg-red-50 p-3 text-sm text-red-800">
              {findings.map((finding, index) => (
                <li key={index}>{finding.message}</li>
              ))}
            </ul>
          ) : null}

          <button
            type="button"
            disabled={publishing || !changeNote.trim()}
            onClick={publish}
            className="rounded bg-accent px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
          >
            {publishing ? "Publishing…" : "Publish"}
          </button>
          {!changeNote.trim() ? (
            <p className="text-xs text-muted">
              A reason is required — the next curator reads it.
            </p>
          ) : null}
        </section>
      ) : null}

      {runId ? <RunProgress runId={runId} /> : null}

      <section>
        <button
          type="button"
          onClick={() => setTechnical(!technical)}
          className="text-sm text-muted underline"
        >
          {technical ? "Hide technical detail" : "Show technical detail"}
        </button>
        {technical && concept.technical ? (
          <dl className="mt-2 space-y-1 text-xs text-muted">
            <dt>Identifier</dt>
            <dd className="font-mono">{concept.technical.iri}</dd>
          </dl>
        ) : null}
      </section>
    </article>
  );
}

/**
 * One polling hook for every kind of run (ADR-005 §5).
 *
 * 2.5 seconds, and it keeps polling across a reload because the moment a user
 * is most likely to reload is the moment they are waiting on something.
 */
function RunProgress({ runId }: { runId: string }) {
  const [run, setRun] = useState<{ status: string; summary?: string } | null>(null);

  useEffect(() => {
    let live = true;
    const tick = async () => {
      const response = await fetch(`/api/runs/${runId}`);
      if (!live || !response.ok) return;
      const body = await response.json();
      setRun(body);
      if (body?.status === "running" || body?.status === "queued") setTimeout(tick, 2500);
    };
    tick();
    return () => {
      live = false;
    };
  }, [runId]);

  return (
    <section className="rounded border border-line bg-slate-50 p-4 text-sm">
      <p className="font-medium">Published.</p>
      <p className="mt-1 text-muted">
        {run?.summary ?? (run?.status === "succeeded" ? "Done." : "Working through the change…")}
      </p>
    </section>
  );
}
