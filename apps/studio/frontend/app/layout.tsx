import type { Metadata } from "next";
import type { ReactNode } from "react";
import { NextIntlClientProvider } from "next-intl";
import { getLocale, getMessages, getTranslations } from "next-intl/server";
import Shell from "@/components/Shell";
import { ToastProvider } from "@/components/Toast";
import "./globals.css";

export const metadata: Metadata = {
  title: "Lion Studio",
  description: "Lion Studio orchestration observability",
};

export default async function RootLayout({
  children,
}: Readonly<{
  children: ReactNode;
}>) {
  const locale = await getLocale();
  const messages = await getMessages();
  const t = await getTranslations({ locale, namespace: "common" });

  return (
    <html lang={locale} suppressHydrationWarning>
      <head>
        {/* Prevent FOUC: read localStorage before paint, default to light */}
        <script
          dangerouslySetInnerHTML={{
            __html: `(function(){var t=localStorage.getItem('theme');if(t==='dark'){document.documentElement.classList.add('dark');}})();`,
          }}
        />
      </head>
      <body className="min-h-screen bg-surface-base font-mono text-content-primary">
        <a
          href="#main-content"
          className="sr-only focus:not-sr-only focus:absolute focus:z-[100] focus:m-2 focus:rounded focus:border focus:border-edge focus:bg-surface-nav focus:px-4 focus:py-2 focus:text-content-primary"
        >
          {t("skipToMain")}
        </a>
        <NextIntlClientProvider messages={messages}>
          <ToastProvider>
            <Shell>{children}</Shell>
          </ToastProvider>
        </NextIntlClientProvider>
      </body>
    </html>
  );
}
