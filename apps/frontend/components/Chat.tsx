"use client";

import Link from "next/link";
import { useState } from "react";

type ConceptChip = {
  origin: "resolution" | "miss" | "fleet_signal";
  surface_form: string;
  concept_id?: string | null;
  pref_label?: string | null;
  gap_id?: string | null;
};

type ConflictItem = {
  concept_id: string;
  predicate: string;
  claims: { value?: string; source_file?: string; doc_version?: string }[];
};

type WireEvent =
  | { type: "text_delta"; text: string }
  | { type: "concept"; item: ConceptChip }
  | { type: "conflict"; item: ConflictItem }
  | { type: "error"; message: string }
  | { type: "done" };

type Turn = { question: string; text: string; concepts: ConceptChip[]; conflicts: ConflictItem[] };

/**
 * The chat surface.
 *
 * The chips beside an answer are not decoration and are not written by the
 * model: they are derived from the retrieval, so they cannot be conjured and
 * cannot be suppressed. The `miss` chip is the important one — README step 1 is
 * "finds nothing, says so honestly", and without it an honest failure looks
 * exactly like a bad answer.
 *
 * A miss shows only that it is a miss. The nearest existing terms are a
 * curator's artefact and belong on the queue entry, not here: as a suffix to
 * an answer they read as "did you mean", which is a claim the similarity score
 * cannot support on this vocabulary.
 *
 * A term the sanitiser corrected renders as an ordinary resolution —
 * "installtion → Installation" — because the left side is always what was
 * typed. There is no separate corrected state to render, by design.
 */
export function Chat() {
  const [question, setQuestion] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [asking, setAsking] = useState(false);
  const [sessionId] = useState(() => `chat-${Math.random().toString(36).slice(2, 10)}`);

  async function ask() {
    const asked = question.trim();
    if (!asked) return;
    setAsking(true);
    setQuestion("");

    const turn: Turn = { question: asked, text: "", concepts: [], conflicts: [] };
    const response = await fetch("/api/chat", {
      method: "POST",
      body: JSON.stringify({ question: asked, session_id: sessionId }),
    });

    if (!response.ok) {
      turn.text = "Something went wrong reaching the documents. Try again.";
      setTurns((previous) => [...previous, turn]);
      setAsking(false);
      return;
    }

    const body = (await response.json()) as { events: WireEvent[] };
    for (const event of body.events) {
      // Unknown event types are ignored, so the agent can add new ones without
      // breaking an already-deployed frontend.
      switch (event.type) {
        case "text_delta":
          turn.text += event.text;
          break;
        case "concept":
          turn.concepts.push(event.item);
          break;
        case "conflict":
          turn.conflicts.push(event.item);
          break;
        case "error":
          turn.text = event.message;
          break;
        default:
          break;
      }
    }

    setTurns((previous) => [...previous, turn]);
    setAsking(false);
  }

  return (
    <div className="space-y-6">
      <div className="space-y-6">
        {turns.map((turn, index) => (
          <article key={index} className="space-y-3">
            <p className="font-medium">{turn.question}</p>

            {turn.concepts.length > 0 ? (
              <ul className="flex flex-wrap gap-2">
                {turn.concepts.map((chip, position) => (
                  <li key={position}>
                    {chip.origin === "miss" ? (
                      <span className="inline-flex items-center gap-1 rounded-full border border-dashed border-held px-3 py-1 text-xs text-held">
                        {chip.surface_form} → not in the vocabulary
                      </span>
                    ) : (
                      <Link
                        href={`/vocabulary/${chip.concept_id}`}
                        className="inline-flex items-center rounded-full border border-line bg-white px-3 py-1 text-xs hover:border-accent"
                      >
                        {chip.surface_form} → <strong className="ml-1">{chip.pref_label}</strong>
                      </Link>
                    )}
                  </li>
                ))}
              </ul>
            ) : null}

            <div className="whitespace-pre-wrap rounded-lg border border-line bg-white p-4 text-sm">
              {turn.text}
            </div>

            {turn.conflicts.map((conflict, position) => (
              <div
                key={position}
                className="rounded-lg border border-held/40 bg-amber-50 p-4 text-sm"
              >
                <p className="font-medium text-held">The documents disagree</p>
                <ul className="mt-2 space-y-1">
                  {conflict.claims.map((side, sideIndex) => (
                    <li key={sideIndex}>
                      {side.value}{" "}
                      <span className="text-muted">
                        — {side.source_file}
                        {side.doc_version ? ` (${side.doc_version})` : ""}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </article>
        ))}
      </div>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          void ask();
        }}
        className="flex gap-2"
      >
        <input
          aria-label="Ask about the dispensers"
          placeholder="How often do I replace the carbon filter?"
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          className="w-full rounded border border-line bg-white px-3 py-2 text-sm"
        />
        <button
          type="submit"
          disabled={asking || !question.trim()}
          className="rounded bg-accent px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
        >
          {asking ? "Looking…" : "Ask"}
        </button>
      </form>
    </div>
  );
}
