import { render, cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { GapCard } from "@/components/GapCard";
import type { GapEntry } from "@/lib/types";

afterEach(cleanup);

/**
 * The shortlist as the deployed index actually returns it for the entry the
 * demonstrator turns on: the metric that shares the word "bottles" first, the
 * actual object second. Fixed in this order deliberately -- a test whose right
 * answer is in position one cannot fail when position one becomes special again.
 */
const gap: GapEntry = {
  gap_id: "gas bottle",
  surface_form: "gas bottle",
  gap_type: "AddAltLabel",
  counts: { ingestion: 6 },
  total_occurrences: 6,
  variants: ["gas bottle"],
  evidence: [
    {
      source: "ingestion",
      text: "Close the regulator on the gas bottle before working on the manifold.",
      locator: "02-service-bulletin-SB-2024-03.md:43-43",
      doc_version: "SB-2024-03",
      occurred_at: "2026-08-11T09:00:00",
    },
  ],
  suggestions: [
    { concept_id: "bottles-avoided", pref_label: "Single-Use Bottles Avoided", score: 0.348 },
    { concept_id: "co2-cylinder", pref_label: "CO₂ Cylinder", score: 0.28 },
    { concept_id: "dispenser", pref_label: "Dispenser", score: 0.2712 },
  ],
  status: "open",
};

/**
 * ADR-004 §2 designed the shortlist as five candidates and a judgement. The card
 * rendered it as one recommendation plus two also-rans, which is a stronger claim
 * than cosine similarity over bare label strings can support -- and on this entry
 * the recommendation was wrong. These two assertions are what stop that returning
 * as a visual-hierarchy improvement.
 */
describe("the gap card offers candidates, not a recommendation", () => {
  it("makes every candidate actionable, not just the top-scoring one", () => {
    const { container } = render(<GapCard gap={gap} />);
    const links = [...container.querySelectorAll("a")];
    const hrefs = links.map((link) => link.getAttribute("href") ?? "");

    expect(links).toHaveLength(gap.suggestions.length);

    for (const suggestion of gap.suggestions) {
      // The surface form travels with every one of them: it prefills the "Also
      // called" box on whichever concept the curator chooses, so a candidate
      // without it lands the curator on a page with nothing filled in.
      expect(hrefs, `${suggestion.pref_label} is not actionable`).toContain(
        `/vocabulary/${suggestion.concept_id}?add=gas%20bottle`,
      );
    }
  });

  it("claims no ordering and shows no score", () => {
    const { container } = render(<GapCard gap={gap} />);
    const text = container.textContent ?? "";

    // ADR-004's M5 amendment removed the number. These words make the same claim
    // without it, which is worse: there is nothing left for a reader to discount.
    for (const word of [/closest/i, /nearest/i, /\bbest\b/i, /\btop\b/i, /recommend/i, /\brank/i]) {
      expect(text, `${word} implies an ordering the shortlist cannot support`).not.toMatch(word);
    }

    for (const suggestion of gap.suggestions) {
      expect(text).not.toContain(String(suggestion.score));
    }
  });

  it("says so plainly when nothing in the vocabulary is close", () => {
    const { container } = render(<GapCard gap={{ ...gap, suggestions: [] }} />);

    expect(container.textContent).toContain("Nothing in the vocabulary looks like this");
    expect(container.querySelectorAll("a")).toHaveLength(0);
  });
});
