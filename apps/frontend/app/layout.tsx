import type { Metadata } from "next";
import Link from "next/link";

import "./globals.css";

export const metadata: Metadata = {
  title: "h2o — vocabulary console",
  description: "Curate the vocabulary, and see what changing it does.",
  icons: { icon: "/favicon.ico", apple: "/apple-touch-icon.png" },
  manifest: "/site.webmanifest",
};

const NAV = [
  ["/vocabulary", "Vocabulary"],
  ["/gaps", "Gaps"],
  ["/runs", "Runs"],
];

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen">
        <header className="border-b border-[--color-line] bg-white">
          <nav className="mx-auto flex max-w-5xl items-center gap-6 px-6 py-4">
            <Link href="/" className="font-semibold tracking-tight">
              h2o
            </Link>
            {NAV.map(([href, label]) => (
              <Link key={href} href={href} className="text-sm text-[--color-muted] hover:text-[--color-accent]">
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
