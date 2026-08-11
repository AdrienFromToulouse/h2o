/**
 * ADR-006 §2: the interface speaks the domain expert's language, not SKOS.
 *
 * The mapping is data rather than scattered string literals so that it can be
 * reviewed in one place and asserted in one test. An expert curating a water
 * dispenser vocabulary should never have to learn what `skos:altLabel` means in
 * order to add the word their technicians actually use.
 */
export const LABELS = {
  prefLabel: "Preferred term",
  altLabel: "Also called",
  hiddenLabel: "Common misspelling",
  definition: "Definition",
  scopeNote: "When to use it",
  changeNote: "Reason for this change",
  broader: "Part of",
  narrower: "Includes",
  related: "See also",
  inScheme: "Vocabulary",
  notation: "Code",
  versionInfo: "Version",
  deprecated: "Retired",
  isReplacedBy: "Replaced by",
} as const;

export const SCHEME_TITLES: Record<string, string> = {
  equipment: "Equipment",
  fault: "Faults",
  service: "Service",
  sustainability: "Sustainability",
  treatment: "Treatment",
  "water-output": "Water output",
  telemetry: "Machine signals",
};

/**
 * Anything here appearing in the default view is a bug, not a style problem.
 *
 * ADR-006 claims the no-jargon rule "is testable". This list is what makes that
 * true: `tests/no-jargon.test.tsx` renders the real components with the
 * technical toggle off and asserts none of these survive into the text. An IRI
 * or a `skos:` prefix reaching a curator is the interface breaking its own
 * promise, and it happens by accident -- a value rendered raw, a label that
 * fell through a lookup.
 */
export const FORBIDDEN_IN_DEFAULT_VIEW: RegExp[] = [
  /skos:/i,
  /owl:/i,
  /\bdct:/i,
  /@prefix/i,
  /\bSELECT\b/,
  /\bprefLabel\b/,
  /\baltLabel\b/,
  /\bhiddenLabel\b/,
  /https?:\/\//,
  /vocab\.h2o\.example/,
  /h2o:graph/,
  // ADR-003 §3.1: attribute keys and values are the machine's language and stay
  // behind the toggle. Instrument *names* are allowed in the two read-only
  // places ADR-006 names, which is why `component\.type` is here and
  // `dispenser\.` is not.
  /component\.type/,
  /fault\.code/,
];

export function offendingJargon(text: string): string[] {
  return FORBIDDEN_IN_DEFAULT_VIEW.filter((pattern) => pattern.test(text)).map(String);
}
