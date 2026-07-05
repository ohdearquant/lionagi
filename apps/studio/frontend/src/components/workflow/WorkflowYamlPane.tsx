import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "use-intl";
import { specToYaml, yamlToSpec } from "@/lib/workflow/serialize";
import { useWorkflowDraft } from "./WorkflowDraftContext";

/**
 * Text half of the designer: the same workflow the canvas shows, as canonical
 * YAML. Canvas edits regenerate the text; hand edits apply back to the canvas
 * once they parse. While the textarea holds unapplied edits the pane is
 * "detached" and canvas changes stop overwriting it until Apply or Discard.
 */
export default function WorkflowYamlPane() {
  const t = useTranslations("workflow");
  const { state, reset } = useWorkflowDraft();
  const { spec } = state;

  const [text, setText] = useState(() => specToYaml(spec));
  const [detached, setDetached] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);
  const lastGeneratedRef = useRef(text);

  useEffect(() => {
    if (detached) return;
    const next = specToYaml(spec);
    if (next !== lastGeneratedRef.current) {
      lastGeneratedRef.current = next;
      setText(next);
      setErrors([]);
    }
  }, [spec, detached]);

  const handleChange = useCallback((value: string) => {
    setText(value);
    setDetached(value !== lastGeneratedRef.current);
  }, []);

  const handleApply = useCallback(() => {
    const result = yamlToSpec(text);
    if (!result.spec) {
      setErrors(result.errors);
      return;
    }
    setErrors([]);
    setDetached(false);
    lastGeneratedRef.current = specToYaml(result.spec);
    setText(lastGeneratedRef.current);
    reset(result.spec);
  }, [text, reset]);

  const handleDiscard = useCallback(() => {
    const next = specToYaml(spec);
    lastGeneratedRef.current = next;
    setText(next);
    setErrors([]);
    setDetached(false);
  }, [spec]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center gap-2 border-b border-edge px-3 py-1.5">
        <span className="font-ui text-[length:var(--t-xs)] font-semibold uppercase tracking-[0.09em] text-content-muted">
          {t("yamlPaneTitle")}
        </span>
        <div className="flex-1" />
        {detached && (
          <>
            <button
              type="button"
              onClick={handleDiscard}
              className="rounded px-2 py-0.5 text-[length:var(--t-xs)] text-content-muted transition-colors hover:text-content-primary"
            >
              {t("yamlDiscard")}
            </button>
            <button
              type="button"
              onClick={handleApply}
              className="rounded px-2.5 py-0.5 text-[length:var(--t-xs)] font-medium transition-colors"
              style={{
                background: "var(--accent)",
                color: "var(--surface-base)",
                border: "1px solid var(--edge)",
              }}
            >
              {t("yamlApply")}
            </button>
          </>
        )}
      </div>

      {errors.length > 0 && (
        <div className="shrink-0 border-b border-edge bg-surface-raised px-3 py-1.5">
          {errors.slice(0, 5).map((err, i) => (
            <p key={i} className="text-[length:var(--t-xs)] text-status-failure">
              {err}
            </p>
          ))}
          {errors.length > 5 && (
            <p className="text-[length:var(--t-xs)] text-content-muted">
              {t("yamlMoreErrors", { count: errors.length - 5 })}
            </p>
          )}
        </div>
      )}

      <textarea
        value={text}
        onChange={(e) => handleChange(e.target.value)}
        spellCheck={false}
        aria-label={t("yamlPaneTitle")}
        className="min-h-0 flex-1 resize-none bg-surface-base p-3 font-data text-[length:var(--t-xs)] leading-relaxed text-content-primary focus:outline-none"
      />
    </div>
  );
}
