import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { GapCard } from "@/components/GapCard";
import { ReviewCard } from "@/components/ReviewCard";
import { FORBIDDEN_IN_DEFAULT_VIEW } from "@/lib/labels";
import type { ConceptDetail, GapEntry } from "@/lib/types";

afterEach(cleanup);

const concept: ConceptDetail = {
  concept_id: "carbon-filter",
  pref_label: { en: "Carbon Filter" },
  alt_labels: ["Carbon Cartridge", "Filter Cartridge"],
  definition: { en: "A filter that removes chlorine and organic compounds by adsorption." },
  parent: { concept_id: "filter", pref_label: "Filter" },
  machine_signals: [{ signal: "dispenser.filter.life_remaining", unit: "%" }],
  scheme_id: "equipment",
  version: 1,
  technical: { iri: "https://vocab.h2o.example/id/carbon-filter" },
};

const gap: GapEntry = {
  gap_id: "gas bottle",
  surface_form: "gas bottle",
  gap_type: "AddAltLabel",
  counts: { ingestion: 12 },
  total_occurrences: 12,
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
  suggestions: [{ concept_id: "co2-cylinder", pref_label: "CO₂ Cylinder", score: 0.28 }],
  status: "open",
};

/**
 * ADR-006 claims the no-jargon rule "is testable". This is that claim, kept.
 *
 * The rule is not a style preference: an IRI or a `skos:` prefix reaching a
 * domain expert means the interface has broken the promise the whole console
 * rests on, and it happens by accident -- a value rendered raw, a label that
 * fell through a lookup table.
 */
describe("the default view speaks the expert's language", () => {
  it("renders a review card with no vocabulary jargon in it", () => {
    const { container } = render(<ReviewCard concept={concept} />);
    const text = container.textContent ?? "";

    for (const pattern of FORBIDDEN_IN_DEFAULT_VIEW) {
      expect(text, `${pattern} reached the default view`).not.toMatch(pattern);
    }
  });

  it("renders a gap card with no vocabulary jargon in it", () => {
    const { container } = render(<GapCard gap={gap} />);
    const text = container.textContent ?? "";

    for (const pattern of FORBIDDEN_IN_DEFAULT_VIEW) {
      expect(text, `${pattern} reached the default view`).not.toMatch(pattern);
    }
  });

  it("shows the identifier only once the toggle is opened", () => {
    const { container } = render(<ReviewCard concept={concept} />);
    expect(container.textContent).not.toContain("vocab.h2o.example");

    fireEvent.click(screen.getByText("Show technical detail"));

    expect(container.textContent).toContain("https://vocab.h2o.example/id/carbon-filter");
  });

  it("names an instrument but never an attribute key", () => {
    // ADR-006 allows the signal *name* in exactly this read-only row. What it
    // forbids outside the toggle is the attribute keys and values --
    // `component.type`, `fault.code=E42` -- which are ADR-003's leakage rule.
    const { container } = render(<ReviewCard concept={concept} />);
    const text = container.textContent ?? "";

    expect(text).toContain("dispenser.filter.life_remaining");
    expect(text).not.toMatch(/component\.type/);
    expect(text).not.toMatch(/fault\.code/);
  });
});

describe("the technical toggle is not a mode", () => {
  it("closes again when the card moves to another term", () => {
    // The real mechanic: in the App Router this component is reused across
    // /vocabulary/a -> /vocabulary/b, so state here would survive the
    // navigation. The page passes `key={conceptId}` to force a remount, and
    // this asserts the behaviour that depends on it.
    const { container, rerender, unmount } = render(
      <ReviewCard key="carbon-filter" concept={concept} />,
    );
    fireEvent.click(screen.getByText("Show technical detail"));
    expect(container.textContent).toContain("Hide technical detail");

    unmount();
    const next = render(
      <ReviewCard
        key="co2-cylinder"
        concept={{ ...concept, concept_id: "co2-cylinder", pref_label: { en: "CO₂ Cylinder" } }}
      />,
    );

    expect(next.container.textContent).toContain("Show technical detail");
    expect(next.container.textContent).not.toContain("Hide technical detail");
    rerender;
  });
});
