import { createRootRoute, Outlet } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { IntlProvider } from "use-intl";
import AppShell from "@/components/shell/AppShell";
import NoDaemonGate from "@/components/shell/NoDaemonGate";
import "../globals.css";

import enMessages from "@/messages/en.json";
import zhMessages from "@/messages/zh.json";

const VALID_LOCALES = ["en", "zh"] as const;
type Locale = (typeof VALID_LOCALES)[number];

const MESSAGES: Record<Locale, typeof enMessages> = {
  en: enMessages,
  zh: zhMessages,
};

function getLocaleFromCookie(): Locale {
  const raw = document.cookie
    .split(";")
    .map((c) => c.trim())
    .find((c) => c.startsWith("NEXT_LOCALE="))
    ?.split("=")[1];
  return (VALID_LOCALES as readonly string[]).includes(raw ?? "") ? (raw as Locale) : "en";
}

function RootComponent() {
  const [locale, setLocale] = useState<Locale>(getLocaleFromCookie);
  const messages = MESSAGES[locale];

  useEffect(() => {
    document.documentElement.lang = locale;
  }, [locale]);

  function handleLocaleChange(next: string) {
    if ((VALID_LOCALES as readonly string[]).includes(next)) {
      setLocale(next as Locale);
    }
  }

  return (
    <IntlProvider locale={locale} messages={messages}>
      <NoDaemonGate>
        <AppShell locale={locale} onLocaleChange={handleLocaleChange}>
          <Outlet />
        </AppShell>
      </NoDaemonGate>
    </IntlProvider>
  );
}

export const Route = createRootRoute({
  component: RootComponent,
});
