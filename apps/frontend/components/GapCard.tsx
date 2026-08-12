import Link from "next/link";

import type { GapEntry } from "@/lib/types";

/**
 * One entry in the queue: a report with candidate attachment points.
 *
 * ADR-004 is explicit that this is *not* a drafted concept. It shows what was
 * said, how often, and where — and then it stops, because a generated draft
 * arrives pre-justified and turns review into arguing with a proposal rather
 * than exercising judgement.
 *
 * **The candidates are peers, and the layout has to keep them that way.** This
 * card used to render the top-scoring one as a link and the rest as muted text,
 * under the words "Closest existing term". Both halves of that were a claim the
 * mechanism never makes: the shortlist is cosine similarity over *bare label
 * strings*, so on this vocabulary it returns lexical overlap, and for the entry
 * the demonstrator turns on it put `Single-Use Bottles Avoided` (0.348, shares
 * the word "bottles") ahead of `CO₂ Cylinder` (0.28, is the actual object). A
 * curator therefore had one clickable candidate and it was the wrong one; the
 * right one was three words of grey text.
 *
 * ADR-004's M5 amendment already removed the *score* from display for exactly
 * this reason. The word "closest" and the styling carried the same claim without
 * the number that would have let a reader discount it. So: no ordering language,
 * no score, and every candidate the same link. Ranking them well is a separate,
 * unbuilt decision (ADR-004 §3) — do not reintroduce the implication here as a
 * visual-hierarchy improvement.
 */
export function GapCard({ gap }: { gap: GapEntry }) {
  const sources = Object.entries(gap.counts).filter(([, n]) => n > 0);

  return (
    <li className="rounded-lg border border-line bg-white p-5">
      <div className="flex items-baseline justify-between gap-4">
        <h2 className="text-lg font-medium">“{gap.surface_form}”</h2>
        <span className="shrink-0 text-sm text-muted">
          {gap.total_occurrences} {gap.total_occurrences === 1 ? "mention" : "mentions"}
        </span>
      </div>

      <p className="mt-1 text-sm text-muted">
        {sources.map(([source, n]) => `${n} in ${source}`).join(" · ")}
        {gap.variants.length > 1 ? ` · spelled ${gap.variants.length} ways` : ""}
      </p>

      {gap.suggestions.length > 0 ? (
        <div className="mt-3 text-sm">
          <p>This term could belong to one of these:</p>
          <ul className="mt-2 flex flex-wrap gap-2">
            {gap.suggestions.map((suggestion) => (
              <li key={suggestion.concept_id}>
                <Link
                  href={`/vocabulary/${suggestion.concept_id}?add=${encodeURIComponent(gap.surface_form)}`}
                  className="inline-block rounded border border-line px-3 py-1 font-medium text-accent hover:underline"
                >
                  {suggestion.pref_label}
                </Link>
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <p className="mt-3 text-sm text-muted">
          Nothing in the vocabulary looks like this. It may need a new term.
        </p>
      )}

      {gap.evidence.length > 0 ? (
        <details className="mt-3">
          <summary className="cursor-pointer text-sm text-muted">
            Where it was said ({gap.evidence.length})
          </summary>
          <ul className="mt-2 space-y-2">
            {gap.evidence.map((item, index) => (
              <li key={index} className="border-l-2 border-line pl-3 text-sm">
                <p>“{item.text}”</p>
                <p className="mt-0.5 text-xs text-muted">
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
