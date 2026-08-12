import { GapCard } from "@/components/GapCard";
import { get } from "@/lib/api-proxy";
import type { GapEntry } from "@/lib/types";

export const dynamic = "force-dynamic";

export default async function GapsPage() {
  const gaps = await get<GapEntry[]>("/gaps");

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Gaps</h1>
        <p className="mt-2 max-w-2xl text-muted">
          Words the documents and the chat used that the vocabulary does not know. Ordered by how
          often they came up, so the order is something you can check rather than trust.
        </p>
      </div>

      {gaps.length === 0 ? (
        <p className="rounded-lg border border-line bg-white p-5 text-muted">
          Nothing waiting. Every term the sources used is one the vocabulary knows.
        </p>
      ) : (
        <ul className="space-y-3">
          {gaps.map((gap) => (
            <GapCard key={gap.gap_id} gap={gap} />
          ))}
        </ul>
      )}
    </div>
  );
}
