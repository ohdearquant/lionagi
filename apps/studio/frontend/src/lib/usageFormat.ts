// Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
// SPDX-License-Identifier: Apache-2.0

// Shared cost/token display formatting for run and branch usage. Keep this
// the single source of truth: RunDetail (session-level) and RunStepCard
// (branch-level) both import from here so the two never drift apart.

// `null`/`undefined` means the provider did not report a cost, distinct from
// a provider reporting a genuine `0.0` (e.g. a subscription run). The two
// must render differently: absent stays "—", zero renders as "$0.00".
export function formatCostUsd(cost: number | null | undefined): string {
  if (cost == null || !Number.isFinite(cost)) return "—";
  const decimals = Math.abs(cost) > 0 && Math.abs(cost) < 0.01 ? 4 : 2;
  return `$${cost.toFixed(decimals)}`;
}

const TOKEN_COUNT_FORMAT = new Intl.NumberFormat("en-US");

export function formatTokenCount(count: number | null | undefined): string {
  if (count == null || !Number.isFinite(count)) return "—";
  return TOKEN_COUNT_FORMAT.format(count);
}
