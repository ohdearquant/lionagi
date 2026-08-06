import { useEffect, useRef, useState } from "react";
import { Link } from "@tanstack/react-router";
import { useTranslations } from "use-intl";
import { ApiError, getResumeAvailability, resumeRun, type SessionBranch } from "@/lib/api";
import type { ResumeAvailability, RunResumeResponse } from "@/lib/types";
import Button from "@/components/ui/Button";
import { IconArrowRight, IconCheck, IconCopy, IconLaunch } from "@/components/ui/icons";

interface Props {
  runId: string;
  /** session.invocation_kind — dispatches which resume UI renders. `agent`
   * keeps the existing branch+instruction form; play/flow/show-play/fanout
   * replay a checkpoint and take neither. Anything else (including null)
   * is not resumable from here — the same refusal the backend dispatcher
   * (services/run_resume.py _dispatch_resume_by_kind) enforces. */
  invocationKind: string | null;
  branches: SessionBranch[];
  onResumed: (result: RunResumeResponse) => void | Promise<void>;
}

// Mirrors _FLOW_RESUME_KINDS in services/run_resume.py.
const FLOW_RESUME_KINDS = new Set(["play", "flow", "show-play", "fanout"]);

export function shellQuote(value: string): string {
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

export function resumeCommand(branchId: string | null, instruction: string): string {
  const branch = branchId ? shellQuote(branchId) : "<branch-id>";
  const followUp = shellQuote(instruction.trim() || "follow-up");
  return `li agent -r ${branch} --prompt ${followUp}`;
}

function SectionShell({ runId, children }: { runId: string; children: React.ReactNode }) {
  const t = useTranslations("runResume");
  return (
    <section
      aria-labelledby={`resume-run-${runId}`}
      className="rounded-lg border border-edge bg-surface-raised p-3 shadow-[var(--shadow-raised-soft)]"
    >
      <div className="flex items-start gap-3">
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded border border-edge bg-surface-overlay text-accent">
          <IconLaunch size={15} />
        </div>
        <div className="min-w-0 flex-1">
          <h3 id={`resume-run-${runId}`} className="text-label font-semibold text-content-primary">
            {t("title")}
          </h3>
          {children}
        </div>
      </div>
    </section>
  );
}

export default function ResumeRun({ runId, invocationKind, branches, onResumed }: Props) {
  const t = useTranslations("runResume");
  const [availability, setAvailability] = useState<ResumeAvailability | null>(null);
  const [availabilityError, setAvailabilityError] = useState<string | null>(null);
  const [checking, setChecking] = useState(true);

  useEffect(() => {
    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reset stale state before the async precheck; setState only fires in the effect body synchronously, not in callbacks
    setChecking(true);
    setAvailability(null);
    setAvailabilityError(null);
    getResumeAvailability(runId)
      .then((result) => {
        if (!cancelled) setAvailability(result);
      })
      .catch((caught) => {
        if (!cancelled) {
          setAvailabilityError(caught instanceof Error ? caught.message : t("availabilityFailed"));
        }
      })
      .finally(() => {
        if (!cancelled) setChecking(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- t is stable per locale; re-running on it would re-fetch on every render
  }, [runId]);

  if (checking) {
    return (
      <SectionShell runId={runId}>
        <p className="mt-0.5 text-body leading-relaxed text-content-muted">
          {t("checkingAvailability")}
        </p>
      </SectionShell>
    );
  }

  if (availabilityError) {
    return (
      <SectionShell runId={runId}>
        <p role="alert" className="mt-0.5 text-body leading-relaxed text-status-failure">
          {availabilityError}
        </p>
      </SectionShell>
    );
  }

  // `branch_conflict` is not a refusal. It means the backend could not pick a
  // branch on its own, which is exactly the case the agent form below already
  // handles: it takes the branch list as a prop and renders a selector
  // whenever there is more than one. Treating it as unresumable would hide a
  // control that works and would make a multi-branch agent run — previously
  // resumable by choosing a branch — unreachable from the UI. Guarded on
  // there actually being a choice to offer, so a conflict with no branches to
  // pick from still reads as the explained refusal below.
  const awaitingBranchChoice =
    invocationKind === "agent" && availability?.reason === "branch_conflict" && branches.length > 1;

  // Resumability is a fact determined before the action is offered — a run
  // with no checkpoint (or any other refusal) reads as an explicit,
  // explained state, never a dead or guessed-at control.
  if (availability && !availability.resumable && !awaitingBranchChoice) {
    return (
      <SectionShell runId={runId}>
        <p className="mt-0.5 text-body leading-relaxed text-content-muted">
          {availability.message || t("notResumable")}
        </p>
      </SectionShell>
    );
  }

  if (invocationKind != null && FLOW_RESUME_KINDS.has(invocationKind)) {
    return <ResumeFlowRun runId={runId} onResumed={onResumed} />;
  }

  return <ResumeAgentForm runId={runId} branches={branches} onResumed={onResumed} />;
}

// ── agent kind: existing branch + instruction form (unchanged behavior) ────

function ResumeAgentForm({
  runId,
  branches,
  onResumed,
}: {
  runId: string;
  branches: SessionBranch[];
  onResumed: Props["onResumed"];
}) {
  const t = useTranslations("runResume");
  const initialBranch = branches.length === 1 ? (branches[0]?.id ?? "") : "";
  const [instruction, setInstruction] = useState("");
  const [branchId, setBranchId] = useState(initialBranch);
  const [showBranchSelector, setShowBranchSelector] = useState(branches.length > 1);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<RunResumeResponse | null>(null);
  const [copied, setCopied] = useState(false);
  const branchSelectRef = useRef<HTMLSelectElement>(null);

  const selectedBranch = branches.find((branch) => branch.id === branchId) ?? null;
  const command = resumeCommand(selectedBranch?.id ?? null, instruction);
  const canResume = Boolean(instruction.trim()) && branches.length > 0 && Boolean(selectedBranch);

  async function handleSubmit() {
    if (!instruction.trim()) {
      setError(t("instructionRequired"));
      return;
    }
    if (branches.length === 0) {
      setError(t("noBranches"));
      return;
    }
    if (!selectedBranch) {
      setShowBranchSelector(true);
      setError(t("chooseBranch"));
      requestAnimationFrame(() => branchSelectRef.current?.focus());
      return;
    }

    setSubmitting(true);
    setError(null);
    setResult(null);
    try {
      const accepted = await resumeRun(runId, {
        instruction: instruction.trim(),
        branch_id: selectedBranch.id,
      });
      setResult(accepted);
      setInstruction("");
      await onResumed(accepted);
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : t("failed");
      if (
        (caught instanceof ApiError && caught.status === 409 && /branch_id/i.test(message)) ||
        /branch_id is required/i.test(message)
      ) {
        setShowBranchSelector(true);
        setError(t("chooseBranch"));
        requestAnimationFrame(() => branchSelectRef.current?.focus());
      } else {
        setError(message);
      }
    } finally {
      setSubmitting(false);
    }
  }

  async function copyCommand() {
    if (!selectedBranch) return;
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      setError(t("copyFailed"));
    }
  }

  return (
    <section
      aria-labelledby={`resume-run-${runId}`}
      className="rounded-lg border border-edge bg-surface-raised p-3 shadow-[var(--shadow-raised-soft)]"
    >
      <div className="flex items-start gap-3">
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded border border-edge bg-surface-overlay text-accent">
          <IconLaunch size={15} />
        </div>
        <div className="min-w-0 flex-1">
          <h3 id={`resume-run-${runId}`} className="text-label font-semibold text-content-primary">
            {t("title")}
          </h3>
          <p className="mt-0.5 text-body leading-relaxed text-content-muted">{t("description")}</p>
        </div>
      </div>

      <div className="mt-3 grid gap-2">
        {(showBranchSelector || branches.length > 1) && (
          <label className="grid gap-1 text-meta font-medium text-content-secondary">
            <span>{t("branch")}</span>
            <select
              ref={branchSelectRef}
              value={branchId}
              onChange={(event) => {
                setBranchId(event.target.value);
                setError(null);
              }}
              className="focus-ring h-8 rounded border border-edge bg-surface-base px-2 text-body text-content-primary"
            >
              <option value="">{t("chooseBranch")}</option>
              {branches.map((branch) => (
                <option key={branch.id} value={branch.id}>
                  {branch.name || branch.id.slice(0, 8)}
                  {branch.model ? ` · ${branch.model}` : ""}
                </option>
              ))}
            </select>
          </label>
        )}

        <label className="grid gap-1 text-meta font-medium text-content-secondary">
          <span>{t("instruction")}</span>
          <textarea
            value={instruction}
            onChange={(event) => {
              setInstruction(event.target.value);
              setError(null);
              setResult(null);
            }}
            onKeyDown={(event) => {
              if (event.nativeEvent.isComposing) return;
              if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
                event.preventDefault();
                void handleSubmit();
              }
            }}
            rows={2}
            maxLength={32_768}
            placeholder={t("placeholder")}
            className="focus-ring min-h-16 resize-y rounded border border-edge bg-surface-base px-2.5 py-2 text-body leading-relaxed text-content-primary placeholder:text-content-muted"
          />
        </label>
      </div>

      {branches.length === 0 && (
        <p className="mt-2 text-meta text-status-failure">{t("noBranches")}</p>
      )}
      {error && (
        <p role="alert" className="mt-2 text-meta text-status-failure">
          {error}
        </p>
      )}
      {result && (
        <div className="mt-2 flex flex-wrap items-center gap-2 rounded border border-status-success/30 bg-status-success-bg px-2.5 py-2 text-body text-status-success">
          <IconCheck size={13} />
          <span className="font-medium">{t("accepted")}</span>
          <Link
            to="/fleet"
            search={{ s: result.run_id, invocation: result.invocation_id }}
            className="focus-ring ms-auto inline-flex items-center gap-1 rounded text-content-primary underline decoration-edge-strong underline-offset-2"
          >
            {t("viewActivity")}
            <span className="max-w-28 truncate font-data text-meta text-content-muted">
              {result.invocation_id}
            </span>
            <IconArrowRight size={12} />
          </Link>
        </div>
      )}

      <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
        <Button
          variant="primary"
          size="sm"
          disabled={!canResume || submitting}
          onClick={() => void handleSubmit()}
          trailing={<IconArrowRight size={12} />}
        >
          {submitting ? t("submitting") : t("submit")}
        </Button>
        <span className="font-data text-meta text-content-muted">{t("shortcut")}</span>
      </div>

      <div className="mt-3 border-t border-edge pt-3">
        <div className="mb-1.5 flex items-center justify-between gap-2">
          <span className="text-meta font-medium text-content-secondary">{t("cli.title")}</span>
          <button
            type="button"
            disabled={!selectedBranch}
            onClick={() => void copyCommand()}
            className="focus-ring inline-flex h-7 items-center gap-1 rounded px-2 text-meta text-content-muted transition-colors hover:bg-surface-overlay hover:text-content-primary disabled:cursor-not-allowed disabled:opacity-40"
          >
            {copied ? <IconCheck size={12} /> : <IconCopy size={12} />}
            {copied ? t("cli.copied") : t("cli.copy")}
          </button>
        </div>
        <code className="block overflow-x-auto rounded border border-edge bg-surface-base px-2.5 py-2 font-data text-meta text-content-secondary">
          {command}
        </code>
        <p className="mt-1.5 text-meta leading-relaxed text-content-muted">{t("cli.help")}</p>
      </div>
    </section>
  );
}

// ── checkpoint-replay kinds (play/flow/show-play/fanout): continue action ──
//
// Unlike the agent path there is no branch to reopen and no instruction to
// give: the checkpoint owns the plan, so this is a single-button "Continue"
// action. If the checkpointed plan itself can't be replayed cleanly (e.g. a
// pending op needs conversational context resume can't restore), that
// surfaces later as a failed invocation with its own reason — visible via
// the run's activity feed, same as any other detached-launch failure.

function ResumeFlowRun({ runId, onResumed }: { runId: string; onResumed: Props["onResumed"] }) {
  const t = useTranslations("runResume");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<RunResumeResponse | null>(null);

  async function submit() {
    setSubmitting(true);
    setError(null);
    setResult(null);
    try {
      const accepted = await resumeRun(runId, {});
      setResult(accepted);
      await onResumed(accepted);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("failed"));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <SectionShell runId={runId}>
      <p className="mt-0.5 text-body leading-relaxed text-content-muted">
        {t("continueDescription")}
      </p>

      {error && (
        <p role="alert" className="mt-2 text-meta text-status-failure">
          {error}
        </p>
      )}

      {result && (
        <div className="mt-2 flex flex-wrap items-center gap-2 rounded border border-status-success/30 bg-status-success-bg px-2.5 py-2 text-body text-status-success">
          <IconCheck size={13} />
          <span className="font-medium">{t("accepted")}</span>
          <Link
            to="/fleet"
            search={{ s: result.run_id, invocation: result.invocation_id }}
            className="focus-ring ms-auto inline-flex items-center gap-1 rounded text-content-primary underline decoration-edge-strong underline-offset-2"
          >
            {t("viewActivity")}
            <span className="max-w-28 truncate font-data text-meta text-content-muted">
              {result.invocation_id}
            </span>
            <IconArrowRight size={12} />
          </Link>
        </div>
      )}

      <div className="mt-3">
        <Button
          variant="primary"
          size="sm"
          disabled={submitting}
          onClick={() => void submit()}
          trailing={<IconArrowRight size={12} />}
        >
          {submitting ? t("submitting") : t("continueSubmit")}
        </Button>
      </div>
    </SectionShell>
  );
}
