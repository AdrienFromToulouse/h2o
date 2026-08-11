import Link from "next/link";

import type { GapEntry } from "@/lib/types";

/**
 * One entry in the queue: a report with a suggested attachment point.
 *
 * ADR-004 is explicit that this is *not* a drafted concept. It shows what was
 * said, how often, and where — and then it stops, because a generated draft
 * arrives pre-justified and turns review into arguing with a proposal rather
 * than exercising judgement.
 */
export function GapCard({ gap }: { gap: GapEntry }) {
  const best = gap.suggestions[0];
  const sources = Object.entries(gap.counts).filter(([, n]) => n > 0);

  return (
    <li className="rounded-lg border border-[--color-line] bg-white p-5">
      <div className="flex items-baseline justify-between gap-4">
        <h2 className="text-lg font-medium">“{gap.surface_form}”</h2>
        <span className="shrink-0 text-sm text-[--color-muted]">
          {gap.total_occurrences} {gap.total_occurrences === 1 ? "mention" : "mentions"}
        </span>
      </div>

      <p className="mt-1 text-sm text-[--color-muted]">
        {sources.map(([source, n]) => `${n} in ${source}`).join(" · ")}
        {gap.variants.length > 1 ? ` · spelled ${gap.variants.length} ways` : ""}
      </p>

      {best ? (
        <p className="mt-3 text-sm">
          Closest existing term:{" "}
          <Link
            href={`/vocabulary/${best.concept_id}?add=${encodeURIComponent(gap.surface_form)}`}
            className="font-medium text-[--color-accent] hover:underline"
          >
            {best.pref_label}
          </Link>
          {gap.suggestions.length > 1 ? (
            <span className="text-[--color-muted]">
              {" "}
              — or {gap.suggestions.slice(1, 3).map((s) => s.pref_label).join(", ")}
            </span>
          ) : null}
        </p>
      ) : (
        <p className="mt-3 text-sm text-[--color-muted]">
          Nothing in the vocabulary looks like this. It may need a new term.
        </p>
      )}

      {gap.evidence.length > 0 ? (
        <details className="mt-3">
          <summary className="cursor-pointer text-sm text-[--color-muted]">
            Where it was said ({gap.evidence.length})
          </summary>
          <ul className="mt-2 space-y-2">
            {gap.evidence.map((item, index) => (
              <li key={index} className="border-l-2 border-[--color-line] pl-3 text-sm">
                <p>“{item.text}”</p>
                <p className="mt-0.5 text-xs text-[--color-muted]">
                  {item.locator ?? item.source}
                  {item.doc_version ? ` · ${item.doc_version}` : ""}
                </p>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </li>
  );
}
