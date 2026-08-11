import Link from "next/link";

export const dynamic = "force-dynamic";

const CARDS = [
  {
    href: "/vocabulary",
    title: "Vocabulary",
    body: "The terms the platform understands, grouped by the vocabulary they belong to.",
  },
  {
    href: "/gaps",
    title: "Gaps",
    body: "Words the documents and the chat used that the vocabulary does not know yet, ordered by how often they came up.",
  },
  {
    href: "/runs",
    title: "Runs",
    body: "What happened after each change: what was reindexed, and how many mentions it resolved.",
  },
];

export default function Home() {
  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Vocabulary console</h1>
        <p className="mt-2 max-w-2xl text-[--color-muted]">
          Adding a term here changes what the platform can answer. Every edit shows what it will do
          before you save it, and what it did afterwards.
        </p>
      </div>
      <div className="grid gap-4 sm:grid-cols-3">
        {CARDS.map((card) => (
          <Link
            key={card.href}
            href={card.href}
            className="rounded-lg border border-[--color-line] bg-white p-5 hover:border-[--color-accent]"
          >
            <h2 className="font-medium">{card.title}</h2>
            <p className="mt-1 text-sm text-[--color-muted]">{card.body}</p>
          </Link>
        ))}
      </div>
    </div>
  );
}
