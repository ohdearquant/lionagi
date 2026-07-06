/** Single source of truth for the 16 supported locales — selector + RTL set. */
export interface LocaleInfo {
  code: string;
  /** Displayed in the language selector, written by a native speaker. */
  native: string;
  /** English name — used in aria-labels and the settings page. */
  english: string;
  dir: "ltr" | "rtl";
}

export const LOCALES: LocaleInfo[] = [
  { code: "en", native: "English", english: "English", dir: "ltr" },
  { code: "zh", native: "中文", english: "Chinese", dir: "ltr" },
  { code: "es", native: "Español", english: "Spanish", dir: "ltr" },
  { code: "fr", native: "Français", english: "French", dir: "ltr" },
  { code: "hi", native: "हिन्दी", english: "Hindi", dir: "ltr" },
  { code: "bn", native: "বাংলা", english: "Bengali", dir: "ltr" },
  { code: "de", native: "Deutsch", english: "German", dir: "ltr" },
  { code: "id", native: "Bahasa Indonesia", english: "Indonesian", dir: "ltr" },
  { code: "pt-BR", native: "Português (Brasil)", english: "Portuguese (Brazil)", dir: "ltr" },
  { code: "ko", native: "한국어", english: "Korean", dir: "ltr" },
  { code: "tr", native: "Türkçe", english: "Turkish", dir: "ltr" },
  { code: "ur", native: "اردو", english: "Urdu", dir: "rtl" },
  { code: "vi", native: "Tiếng Việt", english: "Vietnamese", dir: "ltr" },
  { code: "ar", native: "العربية", english: "Arabic", dir: "rtl" },
  { code: "ru", native: "Русский", english: "Russian", dir: "ltr" },
  { code: "ja", native: "日本語", english: "Japanese", dir: "ltr" },
];

export const RTL_LOCALES: readonly string[] = LOCALES.filter((l) => l.dir === "rtl").map(
  (l) => l.code,
);

/** Applies the active locale to the document: <html lang> + <html dir> for RTL bidi. */
export function applyDocumentLocale(locale: string): void {
  document.documentElement.lang = locale;
  document.documentElement.dir = RTL_LOCALES.includes(locale) ? "rtl" : "ltr";
}
