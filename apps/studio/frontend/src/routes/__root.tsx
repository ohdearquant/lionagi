import { createRootRoute, Outlet } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { IntlProvider } from "use-intl";
import AppShell from "@/components/shell/AppShell";
import NoDaemonGate from "@/components/shell/NoDaemonGate";
import { applyDocumentLocale } from "@/i18n/locales";
import "../globals.css";

import enMessages from "@/messages/en.json";
import zhMessages from "@/messages/zh.json";
import esMessages from "@/messages/es.json";
import frMessages from "@/messages/fr.json";
import hiMessages from "@/messages/hi.json";
import bnMessages from "@/messages/bn.json";
import deMessages from "@/messages/de.json";
import idMessages from "@/messages/id.json";
import ptBRMessages from "@/messages/pt-BR.json";
import koMessages from "@/messages/ko.json";
import trMessages from "@/messages/tr.json";
import urMessages from "@/messages/ur.json";
import viMessages from "@/messages/vi.json";
import arMessages from "@/messages/ar.json";
import ruMessages from "@/messages/ru.json";
import jaMessages from "@/messages/ja.json";

const VALID_LOCALES = [
  "en",
  "zh",
  "es",
  "fr",
  "hi",
  "bn",
  "de",
  "id",
  "pt-BR",
  "ko",
  "tr",
  "ur",
  "vi",
  "ar",
  "ru",
  "ja",
] as const;
type Locale = (typeof VALID_LOCALES)[number];

const MESSAGES: Record<Locale, typeof enMessages> = {
  en: enMessages,
  zh: zhMessages,
  es: esMessages,
  fr: frMessages,
  hi: hiMessages,
  bn: bnMessages,
  de: deMessages,
  id: idMessages,
  "pt-BR": ptBRMessages,
  ko: koMessages,
  tr: trMessages,
  ur: urMessages,
  vi: viMessages,
  ar: arMessages,
  ru: ruMessages,
  ja: jaMessages,
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
    applyDocumentLocale(locale);
  }, [locale]);

  function handleLocaleChange(next: string) {
    if ((VALID_LOCALES as readonly string[]).includes(next)) {
      document.cookie = `NEXT_LOCALE=${next};path=/;max-age=31536000;SameSite=Lax`;
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
