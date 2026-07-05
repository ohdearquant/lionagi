/**
 * Zero state — guided cards for a daemon with no work at all.
 *
 * Rendered only when the first successful fetch confirms the system is
 * empty (no runs, invocations, or schedules). Each card carries a direct
 * CTA into the surface where work starts; once any work exists the real
 * board takes over.
 */

import { Link } from "@tanstack/react-router";
import { useTranslations } from "use-intl";

export default function ZeroState() {
  const t = useTranslations("mission");

  return (
    <section aria-labelledby="zerostate-heading">
      <h2 id="zerostate-heading" className="sr-only">
        {t("zeroState.heading")}
      </h2>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <GuideCard
          title={t("zeroState.agent.title")}
          body={t("zeroState.agent.body")}
          cta={
            <Link to="/library" search={{ tab: "agent" }} className={CTA_CLASS} style={CTA_STYLE}>
              {t("zeroState.agent.cta")}
            </Link>
          }
        />
        <GuideCard
          title={t("zeroState.schedule.title")}
          body={t("zeroState.schedule.body")}
          cta={
            <Link to="/schedules" className={CTA_CLASS} style={CTA_STYLE}>
              {t("zeroState.schedule.cta")}
            </Link>
          }
        />
        <GuideCard
          title={t("zeroState.cli.title")}
          body={t("zeroState.cli.body")}
          cta={
            <code className="block w-fit rounded bg-surface-overlay px-2 py-1 font-data text-[length:var(--t-xs)] text-content-secondary">
              li agent -a researcher &quot;summarize this repo&quot;
            </code>
          }
        />
      </div>
    </section>
  );
}

const CTA_CLASS =
  "inline-block w-fit rounded px-3 py-1.5 font-data text-[length:var(--t-xs)] font-semibold transition-colors duration-100";

const CTA_STYLE = {
  background: "color-mix(in srgb, var(--accent) 12%, transparent)",
  color: "var(--accent)",
  border: "1px solid color-mix(in srgb, var(--accent) 25%, transparent)",
} as const;

function GuideCard({ title, body, cta }: { title: string; body: string; cta: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-2 rounded border border-edge bg-surface-raised px-4 py-4">
      <h3 className="text-[length:var(--t-sm)] font-semibold text-content-primary">{title}</h3>
      <p className="flex-1 text-[length:var(--t-xs)] leading-relaxed text-content-muted">{body}</p>
      {cta}
    </div>
  );
}
