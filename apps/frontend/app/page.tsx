import Link from "next/link";

export const dynamic = "force-dynamic";

const CARDS = [
  {
    href: "/chat",
    title: "Ask",
    body: "Answers come from the documents, with the file and the words they used. When the sources disagree, you get both sides.",
  },
  {
    href: "/vocabulary",
    title: "Vocabulary",
    body: "The terms the platform understands, written by people before anything was ingested.",
  },
  {
    href: "/gaps",
    title: "Gaps",
    body: "Words the documents and the questions used that the vocabulary does not know yet, ordered by how often they came up.",
  },
  {
    href: "/runs",
    title: "Runs",
    body: "What happened after each change: what was reindexed, and how many waiting mentions it resolved.",
  },
];

export default function Home() {
  return (
    <div className="space-y-10">
      <div className="flex flex-col items-center gap-4 text-center">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">AquaKnow</h1>
          <p className="mt-1 text-sm uppercase tracking-[0.2em] text-muted">
            Knowledge · AI · Action
          </p>
        </div>
        <p className="max-w-2xl text-muted">
          People write the vocabulary. The assistant uses it, and reports what it is missing. You
          decide what gets added — and adding a term changes what the platform can answer, with the
          counts to prove it.
        </p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        {CARDS.map((card) => (
          <Link
            key={card.href}
            href={card.href}
            className="rounded-lg border border-line bg-white p-5 hover:border-accent"
          >
            <h2 className="font-medium">{card.title}</h2>
            <p className="mt-1 text-sm text-muted">{card.body}</p>
          </Link>
        ))}
      </div>
    </div>
  );
}
