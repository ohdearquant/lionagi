import { createRootRoute, Outlet } from "@tanstack/react-router";
import { IntlProvider } from "use-intl";
import { ToastProvider } from "@/components/Toast";
import Shell from "@/components/Shell";
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
  const locale = getLocaleFromCookie();
  const messages = MESSAGES[locale];

  return (
    <IntlProvider locale={locale} messages={messages}>
      <ToastProvider>
        <Shell>
          <Outlet />
        </Shell>
      </ToastProvider>
    </IntlProvider>
  );
}

export const Route = createRootRoute({
  component: RootComponent,
});
