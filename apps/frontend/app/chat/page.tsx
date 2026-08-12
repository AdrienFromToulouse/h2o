import { Chat } from "@/components/Chat";

export const dynamic = "force-dynamic";

export default function ChatPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Ask</h1>
        <p className="mt-2 max-w-2xl text-muted">
          Answers come from the documents and nothing else. Where the sources disagree you get every
          side. Where a word isn&apos;t one the documents use, you get told — and it joins the gap
          queue.
        </p>
      </div>
      <Chat />
    </div>
  );
}
