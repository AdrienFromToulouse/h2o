export type SchemeRef = {
  scheme_id: string;
  title: string;
  description?: string | null;
  concept_count: number;
};

export type ConceptRef = { concept_id: string; pref_label: string; definition?: string | null };

export type SchemeTree = { schemes: SchemeRef[]; top_concepts: Record<string, ConceptRef[]> };

export type MachineSignal = {
  signal: string;
  unit?: string | null;
  notation?: string | null;
  match?: string | null;
  scope_note?: string | null;
};

export type ConceptDetail = {
  concept_id: string;
  pref_label: Record<string, string>;
  alt_labels: string[];
  hidden_labels?: string[];
  definition?: Record<string, string> | null;
  scope_note?: string | null;
  parent?: ConceptRef | null;
  children?: ConceptRef[];
  related?: ConceptRef[];
  machine_signals?: MachineSignal[];
  scheme_id?: string | null;
  version: number;
  deprecated?: boolean;
  technical?: { iri: string; scheme_iri?: string; turtle?: string; otel_bindings?: unknown };
};

export type GapEvidence = {
  source: string;
  text: string;
  locator?: string | null;
  doc_version?: string | null;
  occurred_at: string;
};

export type GapEntry = {
  gap_id: string;
  surface_form: string;
  gap_type: string;
  counts: Record<string, number>;
  total_occurrences: number;
  variants: string[];
  evidence: GapEvidence[];
  suggestions: { concept_id: string; pref_label: string; score: number }[];
  status: string;
};

export type Impact = {
  concept_id: string;
  sentence: string;
  mention_count: number;
  document_count: number;
  documents: string[];
  blocked: boolean;
  findings: { concept_id: string; message: string; severity: string }[];
  mentions: {
    surface_form: string;
    source_file: string;
    doc_version: string;
    line_range: string;
    snippet: string;
  }[];
};

export type RunStep = { name: string; counts: Record<string, unknown> };

export type Run = {
  run_id: string;
  kind: string;
  status: string;
  started_at?: string;
  finished_at?: string;
  summary?: string;
  concept_id?: string;
  counts?: Record<string, unknown>;
  steps?: RunStep[];
  error?: string;
};
