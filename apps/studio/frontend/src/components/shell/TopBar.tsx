import { useTranslations } from "use-intl";

export default function TopBar() {
  const t = useTranslations("shell");
  const isTauri = typeof window !== "undefined" && "__TAURI__" in window;

  return (
    <div
      className="flex min-h-8 shrink-0 items-center justify-end border-b border-edge px-3"
      style={{ paddingTop: isTauri ? 40 : undefined }}
    >
      <a
        href="https://khive.ai"
        target="_blank"
        rel="noopener noreferrer"
        title={t("topbar.khiveLink")}
        className="text-[length:var(--t-xs)] text-content-muted transition-colors duration-100 hover:text-content-primary"
      >
        {t("topbar.khiveLink")}
        <span className="sr-only"> ({t("topbar.newTab")})</span>
      </a>
    </div>
  );
}
