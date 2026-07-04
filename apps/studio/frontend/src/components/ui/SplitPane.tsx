/**
 * SplitPane — master-detail layout primitive (DESIGN-SYSTEM §4).
 *
 * Two sibling panes filling the parent: a master list and an always-visible
 * detail pane separated by a drag-resizable hairline. Below 900px container
 * width it collapses to stacked navigation: the master fills the container,
 * and when `detailActive` the detail fills it instead (caller renders a back
 * affordance via `onBack`). No overlays, no open/close ceremony.
 */

import { useCallback, useLayoutEffect, useRef, useState, type ReactNode } from "react";

const COLLAPSE_AT = 900;
const HANDLE_HIT = 8;

interface Props {
  /** Stable id — persists the master width in localStorage. */
  id: string;
  master: ReactNode;
  detail: ReactNode;
  /** Initial master width in px (default 380). */
  defaultMasterWidth?: number;
  minMasterWidth?: number;
  maxMasterWidth?: number;
  /** In collapsed (stacked) mode: show detail instead of master. */
  detailActive?: boolean;
  ariaLabelMaster?: string;
  ariaLabelDetail?: string;
}

function storedWidth(id: string, fallback: number): number {
  if (typeof window === "undefined") return fallback;
  const raw = window.localStorage.getItem(`split:${id}`);
  const n = raw == null ? NaN : Number(raw);
  return Number.isFinite(n) && n > 0 ? n : fallback;
}

export default function SplitPane({
  id,
  master,
  detail,
  defaultMasterWidth = 380,
  minMasterWidth = 260,
  maxMasterWidth = 640,
  detailActive = false,
  ariaLabelMaster,
  ariaLabelDetail,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [masterWidth, setMasterWidth] = useState(() => storedWidth(id, defaultMasterWidth));
  const [collapsed, setCollapsed] = useState(false);
  const dragRef = useRef<{ startX: number; startWidth: number } | null>(null);

  useLayoutEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width ?? 0;
      setCollapsed(w > 0 && w < COLLAPSE_AT);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const clamp = useCallback(
    (w: number) => {
      const containerW = containerRef.current?.clientWidth ?? Infinity;
      // Detail keeps at least 40% of the container.
      const hardMax = Math.min(maxMasterWidth, containerW * 0.6);
      return Math.min(Math.max(w, minMasterWidth), Math.max(hardMax, minMasterWidth));
    },
    [minMasterWidth, maxMasterWidth],
  );

  const onPointerDown = useCallback(
    (e: React.PointerEvent) => {
      dragRef.current = { startX: e.clientX, startWidth: masterWidth };
      (e.target as HTMLElement).setPointerCapture(e.pointerId);
    },
    [masterWidth],
  );

  const onPointerMove = useCallback(
    (e: React.PointerEvent) => {
      const drag = dragRef.current;
      if (!drag) return;
      setMasterWidth(clamp(drag.startWidth + (e.clientX - drag.startX)));
    },
    [clamp],
  );

  const endDrag = useCallback(() => {
    if (!dragRef.current) return;
    dragRef.current = null;
    setMasterWidth((w) => {
      window.localStorage.setItem(`split:${id}`, String(Math.round(w)));
      return w;
    });
  }, [id]);

  // Render-time clamp uses prop bounds only (reading the container ref during
  // render is invalid); container-aware clamping happens in drag/key handlers.
  const effectiveWidth = Math.min(Math.max(masterWidth, minMasterWidth), maxMasterWidth);

  if (collapsed) {
    return (
      <div ref={containerRef} className="flex h-full min-h-0 w-full flex-col">
        {detailActive ? (
          <section aria-label={ariaLabelDetail} className="flex min-h-0 flex-1 flex-col">
            {detail}
          </section>
        ) : (
          <section aria-label={ariaLabelMaster} className="flex min-h-0 flex-1 flex-col">
            {master}
          </section>
        )}
      </div>
    );
  }

  return (
    <div ref={containerRef} className="flex h-full min-h-0 w-full">
      <section
        aria-label={ariaLabelMaster}
        className="flex min-h-0 flex-col overflow-hidden"
        style={{ width: effectiveWidth, flexShrink: 0 }}
      >
        {master}
      </section>
      {/* WAI-ARIA window-splitter pattern: a separator IS interactive (focus +
          arrow keys + drag), which jsx-a11y's static role lists don't model. */}
      {/* eslint-disable jsx-a11y/no-noninteractive-element-interactions, jsx-a11y/no-noninteractive-tabindex */}
      <div
        role="separator"
        aria-orientation="vertical"
        tabIndex={0}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        onKeyDown={(e) => {
          if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
            e.preventDefault();
            const next = clamp(masterWidth + (e.key === "ArrowLeft" ? -16 : 16));
            setMasterWidth(next);
            window.localStorage.setItem(`split:${id}`, String(Math.round(next)));
          }
        }}
        className="group relative flex-shrink-0 cursor-col-resize"
        style={{ width: HANDLE_HIT, marginLeft: -HANDLE_HIT / 2 + 1, marginRight: -HANDLE_HIT / 2 }}
      >
        <div
          aria-hidden
          className="absolute inset-y-0 left-1/2 w-px transition-colors duration-100 group-hover:bg-[var(--accent)] group-focus-visible:bg-[var(--accent)]"
          style={{ background: "var(--edge-hairline)" }}
        />
      </div>
      <section
        aria-label={ariaLabelDetail}
        className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden"
      >
        {detail}
      </section>
    </div>
  );
}
