import type { Metadata } from "next";
import Image from "next/image";
import Link from "next/link";

import "./globals.css";

export const metadata: Metadata = {
  title: "AquaKnow — vocabulary console",
  description: "Curate the vocabulary, and see what changing it does.",
  icons: { icon: "/favicon.ico", apple: "/apple-touch-icon.png" },
  manifest: "/site.webmanifest",
};

const NAV = [
  ["/chat", "Ask"],
  ["/vocabulary", "Vocabulary"],
  ["/gaps", "Gaps"],
  ["/runs", "Runs"],
];

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen">
        <header className="border-b border-line bg-white">
          <nav className="mx-auto flex max-w-5xl items-center gap-6 px-6 py-3">
            <Link href="/" className="flex items-center gap-2">
              <Image src="/aquaknow.png" alt="" width={32} height={32} priority />
              <span className="font-semibold tracking-tight">AquaKnow</span>
            </Link>
            {NAV.map(([href, label]) => (
              <Link
                key={href}
                href={href}
                className="text-sm text-muted hover:text-accent"
              >
                {label}
              </Link>
            ))}
          </nav>
        </header>
        <main className="mx-auto max-w-5xl px-6 py-10">{children}</main>
      </body>
    </html>
  );
}
