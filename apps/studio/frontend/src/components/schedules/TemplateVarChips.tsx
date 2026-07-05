/** Inline chip list of PR template variable names shown below the prompt
 *  field when trigger_type is "github_poll". */
export default function TemplateVarChips({ vars, hint }: { vars: string[]; hint: string }) {
  return (
    <div className="rounded border border-edge bg-surface-base px-3 py-2.5">
      <p className="mb-2 text-meta text-content-secondary">{hint}</p>
      <div className="flex flex-wrap gap-1.5">
        {vars.map((v) => (
          <span
            key={v}
            className="rounded border border-edge bg-surface-raised px-1.5 py-0.5 font-data text-meta text-content-primary"
          >
            {v}
          </span>
        ))}
      </div>
    </div>
  );
}
