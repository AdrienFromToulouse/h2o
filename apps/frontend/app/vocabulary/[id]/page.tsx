import { notFound } from "next/navigation";

import { ReviewCard } from "@/components/ReviewCard";
import { call } from "@/lib/api-proxy";
import type { ConceptDetail } from "@/lib/types";

export const dynamic = "force-dynamic";

export default async function ConceptPage({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ add?: string }>;
}) {
  const { id } = await params;
  const { add } = await searchParams;

  const response = await call(`/vocabulary/concepts/${encodeURIComponent(id)}`);
  if (response.status === 404) notFound();
  const concept = (await response.json()) as ConceptDetail;

  // `key` forces a remount when the id changes. Without it the App Router
  // reuses the component across /vocabulary/a -> /vocabulary/b, and the
  // technical toggle's state would survive the navigation -- becoming the
  // sticky technical mode ADR-006 §2 forbids.
  return <ReviewCard key={id} concept={concept} prefill={add} />;
}
