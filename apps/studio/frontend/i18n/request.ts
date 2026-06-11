import { getRequestConfig } from "next-intl/server";
import { cookies } from "next/headers";

const VALID_LOCALES = ["en", "zh"] as const;
type Locale = (typeof VALID_LOCALES)[number];

export default getRequestConfig(async () => {
  const cookieStore = await cookies();
  const raw = cookieStore.get("NEXT_LOCALE")?.value ?? "en";
  const locale: Locale = (VALID_LOCALES as readonly string[]).includes(raw)
    ? (raw as Locale)
    : "en";

  return {
    locale,
    messages: (await import(`../messages/${locale}.json`)).default,
  };
});
