import type { Metadata } from "next";
import type { ReactNode } from "react";
import Shell from "@/components/Shell";
import "./globals.css";

export const metadata: Metadata = {
  title: "Lion Studio",
  description: "Lion Studio orchestration observability",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: ReactNode;
}>) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-neutral-950 font-mono text-neutral-200">
        <Shell>{children}</Shell>
      </body>
    </html>
  );
}
