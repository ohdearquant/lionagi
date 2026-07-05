/**
 * The single icon vocabulary. Contract (design review feedback):
 * viewBox "0 0 24 24", stroke currentColor, strokeWidth 1.5, round
 * caps/joins, fill none, literal shapes. At render sizes ≤ 12 pass
 * strokeWidth 2.25–2.5 so optical weight stays ~1px.
 */

import type { SVGProps } from "react";

export type IconProps = SVGProps<SVGSVGElement> & { size?: number };

function Icon({ size = 16, children, ...props }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      {children}
    </svg>
  );
}

/* ── Status & feedback ───────────────────────────────────────────────────── */

export function IconCheck(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M5 12.5l4.5 4.5L19 7" />
    </Icon>
  );
}

export function IconClose(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M18 6L6 18M6 6l12 12" />
    </Icon>
  );
}

export function IconDotFilled(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="12" cy="12" r="5" fill="currentColor" stroke="none" />
    </Icon>
  );
}

export function IconDotOutline(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="12" cy="12" r="5" />
    </Icon>
  );
}

/** Half-filled dot — in-flight state. */
export function IconDotHalf(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="12" cy="12" r="5" />
      <path d="M12 7a5 5 0 010 10z" fill="currentColor" stroke="none" />
    </Icon>
  );
}

export function IconPause(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M9 6v12M15 6v12" />
    </Icon>
  );
}

/** Circle-slash — vetoed / not allowed. */
export function IconBan(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="12" cy="12" r="8" />
      <path d="M6.5 6.5l11 11" />
    </Icon>
  );
}

/** Double tilde — approximate / provisional verdict. */
export function IconApprox(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M5 9.5c2-2.2 4.5-2.2 6.5 0s4.5 2.2 6.5 0M5 15c2-2.2 4.5-2.2 6.5 0s4.5 2.2 6.5 0" />
    </Icon>
  );
}

/** Turn-right arrow — merged / landed elsewhere. */
export function IconArrowTurnRight(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M5 19v-7a4 4 0 014-4h9" />
      <path d="M14 4l4 4-4 4" />
    </Icon>
  );
}

export function IconWarning(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
      <path d="M12 9v4M12 17h.01" />
    </Icon>
  );
}

export function IconError(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 8v4M12 16h.01" />
    </Icon>
  );
}

export function IconInfo(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 8h.01M12 12v4" />
    </Icon>
  );
}

/* ── Navigation & disclosure ─────────────────────────────────────────────── */

export function IconChevronRight(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M9 5l7 7-7 7" />
    </Icon>
  );
}

export function IconChevronDown(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M5 9l7 7 7-7" />
    </Icon>
  );
}

export function IconChevronUp(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M5 15l7-7 7 7" />
    </Icon>
  );
}

export function IconChevronLeft(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M15 5l-7 7 7 7" />
    </Icon>
  );
}

export function IconArrowLeft(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M19 12H5M11 18l-6-6 6-6" />
    </Icon>
  );
}

export function IconArrowRight(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M5 12h14M13 6l6 6-6 6" />
    </Icon>
  );
}

/** Up-right arrow — external link / outbound fetch. */
export function IconArrowUpRight(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M7 17L17 7M9 7h8v8" />
    </Icon>
  );
}

export function IconUndo(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M3 7v6h6" />
      <path d="M3 13C5.6 8.3 11.1 6 16 7.6a9 9 0 014 7.4" />
    </Icon>
  );
}

/* ── Objects & tools ─────────────────────────────────────────────────────── */

export function IconFile(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M14 3H7a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V8z" />
      <path d="M14 3v5h5" />
    </Icon>
  );
}

export function IconSearch(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="11" cy="11" r="7" />
      <path d="M21 21l-4.35-4.35" />
    </Icon>
  );
}

export function IconGlobe(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="12" cy="12" r="9" />
      <path d="M3 12h18" />
      <path d="M12 3c2.5 2.4 3.8 5.6 3.8 9s-1.3 6.6-3.8 9c-2.5-2.4-3.8-5.6-3.8-9s1.3-6.6 3.8-9z" />
    </Icon>
  );
}

export function IconTerminal(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M4 17l6-5-6-5" />
      <path d="M12 19h8" />
    </Icon>
  );
}

export function IconPencil(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M17 3a2.83 2.83 0 014 4L7.5 20.5 2 22l1.5-5.5L17 3z" />
    </Icon>
  );
}

export function IconCopy(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="9" y="9" width="12" height="12" rx="2" />
      <path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1" />
    </Icon>
  );
}

/* ── Designer canvas vocabulary ──────────────────────────────────────────── */

/** Step node — rectangle with play arrow inside. */
export function IconStep(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="3" y="6" width="18" height="12" rx="2" />
      <path d="M9 9l4 3-4 3V9z" />
    </Icon>
  );
}

/** Fanout — one source splitting into multiple targets. */
export function IconFanout(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="5" cy="12" r="2" />
      <circle cx="19" cy="7" r="2" />
      <circle cx="19" cy="17" r="2" />
      <path d="M7 12h4l4-5M7 12h4l4 5" />
    </Icon>
  );
}

/** Gate — decision diamond. */
export function IconGate(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 3L21 12 12 21 3 12z" />
    </Icon>
  );
}

/** Join — multiple inputs converging to one. */
export function IconJoin(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="5" cy="7" r="2" />
      <circle cx="5" cy="17" r="2" />
      <circle cx="19" cy="12" r="2" />
      <path d="M7 7l4 5-4 5M7 7l8 5-8 5" />
    </Icon>
  );
}

/** Input — inbox tray. */
export function IconInput(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M3 15h18v4a1 1 0 01-1 1H4a1 1 0 01-1-1v-4z" />
      <path d="M12 3v9m0 0l-3-3m3 3l3-3" />
    </Icon>
  );
}

/** Reaction — lightning bolt event trigger. */
export function IconReaction(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M13 3L4 14h7l0 7 9-11h-7l0-7z" />
    </Icon>
  );
}

/** Debounce — funnel. */
export function IconDebounce(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M4 4h16l-6 8v6l-4-2V12L4 4z" />
    </Icon>
  );
}

/** Run / play. */
export function IconRun(props: IconProps) {
  return (
    <Icon {...props}>
      <polygon points="5,3 19,12 5,21" />
    </Icon>
  );
}

/** Validate — check in shield. */
export function IconValidate(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 3l8 4v5c0 4.4-3.4 8.5-8 10-4.6-1.5-8-5.6-8-10V7l8-4z" />
      <path d="M9 12l2 2 4-4" />
    </Icon>
  );
}

/** YAML / code view toggle. */
export function IconYaml(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M7 8l-4 4 4 4M17 8l4 4-4 4" />
      <path d="M14 4l-4 16" />
    </Icon>
  );
}

/** Presets — template library grid. */
export function IconPresets(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="3" y="3" width="7" height="7" rx="1" />
      <rect x="14" y="3" width="7" height="7" rx="1" />
      <rect x="3" y="14" width="7" height="7" rx="1" />
      <rect x="14" y="14" width="7" height="7" rx="1" />
    </Icon>
  );
}

/** Emission port — circle with outward arrow. */
export function IconPort(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="12" cy="12" r="3" />
      <path d="M15 12h6M18 9l3 3-3 3" />
    </Icon>
  );
}

export function IconPlus(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 5v14M5 12h14" />
    </Icon>
  );
}

export function IconMinus(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M5 12h14" />
    </Icon>
  );
}

/** Fit view — four corner brackets. */
export function IconFitView(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5" />
    </Icon>
  );
}

/** Agent — head and shoulders. */
export function IconAgent(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="12" cy="8" r="3.5" />
      <path d="M4 20c0-4 3.6-7 8-7s8 3 8 7" />
    </Icon>
  );
}

/** Team — two heads, shared shoulders. */
export function IconTeam(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="9" cy="7" r="3" />
      <circle cx="15" cy="7" r="3" />
      <path d="M3 20v-2a4 4 0 014-4h10a4 4 0 014 4v2" />
    </Icon>
  );
}

/** Tool / maintenance — wrench. */
export function IconTool(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z" />
    </Icon>
  );
}

/** Synthesis — converging lines. */
export function IconSynth(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M5 7l7 5 7-5M5 17l7-5 7 5" />
      <circle cx="12" cy="12" r="1" />
    </Icon>
  );
}

/** Shield — judge-gated. */
export function IconShield(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 3l8 4v5c0 4.4-3.4 8.5-8 10-4.6-1.5-8-5.6-8-10V7l8-4z" />
    </Icon>
  );
}

export function IconSave(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M19 21H5a2 2 0 01-2-2V5a2 2 0 012-2h11l5 5v11a2 2 0 01-2 2z" />
      <polyline points="17 21 17 13 7 13 7 21" />
      <polyline points="7 3 7 8 15 8" />
    </Icon>
  );
}

/** Launch — rocket. */
export function IconLaunch(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 00-2.91-.09z" />
      <path d="M12 15l-3-3a22 22 0 012-3.95A12.88 12.88 0 0122 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 01-4 2z" />
      <path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5" />
    </Icon>
  );
}

/* ── Entity kinds ────────────────────────────────────────────────────────── */

/** Invocation — burst rays. */
export function IconInvocation(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83" />
    </Icon>
  );
}

/** Show — stage monitor. */
export function IconShow(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="2" y="3" width="20" height="14" rx="2" />
      <line x1="8" y1="21" x2="16" y2="21" />
      <line x1="12" y1="17" x2="12" y2="21" />
    </Icon>
  );
}

/** Playbook — ruled document. */
export function IconPlaybook(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="4" y="3" width="16" height="18" rx="2" />
      <line x1="8" y1="8" x2="16" y2="8" />
      <line x1="8" y1="12" x2="16" y2="12" />
      <line x1="8" y1="16" x2="12" y2="16" />
    </Icon>
  );
}

/** Skill — star. */
export function IconSkill(props: IconProps) {
  return (
    <Icon {...props}>
      <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2" />
    </Icon>
  );
}

/** Plugin — stacked layers. */
export function IconPlugin(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 2L2 7l10 5 10-5-10-5z" />
      <path d="M2 17l10 5 10-5" />
      <path d="M2 12l10 5 10-5" />
    </Icon>
  );
}

/** Signal — sine wave. */
export function IconSignal(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M2 12c1.5-4 3-4 4.5 0s3 4 4.5 0 3-4 4.5 0 3 4 4.5 0" />
    </Icon>
  );
}

/** Trash — delete. */
export function IconTrash(props: IconProps) {
  return (
    <Icon {...props}>
      <polyline points="3 6 5 6 21 6" />
      <path d="M19 6l-1 14H6L5 6" />
      <path d="M10 11v6M14 11v6" />
      <path d="M9 6V4h6v2" />
    </Icon>
  );
}

/** Engine — hub with rays. */
export function IconEngine(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="12" cy="12" r="3" />
      <path d="M12 1v4M12 19v4M4.22 4.22l2.83 2.83M16.95 16.95l2.83 2.83M1 12h4M19 12h4M4.22 19.78l2.83-2.83M16.95 7.05l2.83-2.83" />
    </Icon>
  );
}

/* ── System ──────────────────────────────────────────────────────────────── */

/** Health — pulse line. */
export function IconHealth(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M22 12h-4l-3 9L9 3l-3 9H2" />
    </Icon>
  );
}

/** Schedule — clock. */
export function IconSchedule(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="12" cy="12" r="10" />
      <polyline points="12 6 12 12 16 14" />
    </Icon>
  );
}

/** Settings — gear. */
export function IconSettings(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z" />
    </Icon>
  );
}
