import Link from "next/link";

import { get } from "@/lib/api-proxy";
import { SCHEME_TITLES } from "@/lib/labels";
import type { SchemeTree } from "@/lib/types";

export const dynamic = "force-dynamic";

export default async function VocabularyPage() {
  // The machine scheme is not requested. ADR-006 keeps firmware's names for
  // things out of the curation interface, and the cheapest way to keep them out
  // is not to ask for them.
  const tree = await get<SchemeTree>("/vocabulary");

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Vocabulary</h1>
        <p className="mt-2 text-[--color-muted]">
          {tree.schemes.length} vocabularies, {tree.schemes.reduce((n, s) => n + s.concept_count, 0)}{" "}
          terms. Written by people, before anything was ingested.
        </p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        {tree.schemes.map((scheme) => (
          <section key={scheme.scheme_id} className="rounded-lg border border-[--color-line] bg-white p-5">
            <h2 className="font-medium">{SCHEME_TITLES[scheme.scheme_id] ?? scheme.title}</h2>
            <p className="mt-1 text-sm text-[--color-muted]">{scheme.concept_count} terms</p>
            <ul className="mt-3 space-y-1">
              {(tree.top_concepts[scheme.scheme_id] ?? []).map((concept) => (
                <li key={concept.concept_id}>
                  <Link
                    href={`/vocabulary/${concept.concept_id}`}
                    className="text-sm text-[--color-accent] hover:underline"
                  >
                    {concept.pref_label}
                  </Link>
                </li>
              ))}
            </ul>
          </section>
        ))}
      </div>
    </div>
  );
}
