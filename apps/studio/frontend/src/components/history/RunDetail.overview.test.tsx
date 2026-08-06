// Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
// SPDX-License-Identifier: Apache-2.0

/**
 * RunDetail: session Overview cost/token wiring.
 *
 * The Overview section reads its cost/token stats from SessionDetail
 * (input_tokens, output_tokens, total_cost_usd), not from a per-branch
 * sum computed client-side. This renders the full component through the
 * real API contract so a broken or removed mapping fails here, instead of
 * only in a test that hands RunStepCard a pre-built result object.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import enMessages from "@/messages/en.json";
import type { SessionDetail } from "@/lib/api";

class MockApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

const getSessionMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", () => ({
  ApiError: MockApiError,
  getSession: getSessionMock,
  getInvocation: vi.fn().mockRejectedValue(new Error("no invocation in this test")),
  streamSession: vi.fn(() => () => {}),
  streamSignals: vi.fn(() => () => {}),
  resumeRun: vi.fn(),
  // Never resolves: these tests are about the Overview stats, not resume state.
  getResumeAvailability: vi.fn(() => new Promise(() => {})),
}));

const { default: RunDetail } = await import("./RunDetail");

function baseSession(overrides: Partial<SessionDetail> = {}): SessionDetail {
  return {
    id: "s1",
    name: "session one",
    created_at: 1,
    updated_at: 1,
    status: "completed",
    branches: [],
    ...overrides,
  } as SessionDetail;
}

describe("RunDetail: Overview renders session-level cost/token totals", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    getSessionMock.mockReset();
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    Element.prototype.scrollIntoView = vi.fn();
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  async function mount() {
    await act(async () => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <RunDetail id="s1" />
        </IntlProvider>,
      );
      await Promise.resolve();
      await Promise.resolve();
    });
  }

  it("renders the session's sub-cent cost and thousands-separated token counts from SessionDetail", async () => {
    getSessionMock.mockResolvedValueOnce(
      baseSession({
        total_cost_usd: 0.0003,
        input_tokens: 12345,
        output_tokens: 6789,
        branches: [],
      }),
    );

    await mount();

    expect(container.textContent).toContain("$0.0003");
    expect(container.textContent).toContain("12,345");
    expect(container.textContent).toContain("6,789");
  });

  it("renders unreported session cost as the not-reported glyph, distinct from a genuine zero", async () => {
    getSessionMock.mockResolvedValueOnce(
      baseSession({
        total_cost_usd: null,
        input_tokens: null,
        output_tokens: null,
        branches: [],
      }),
    );

    await mount();

    expect(container.textContent).toContain("—");
    expect(container.textContent).not.toContain("$0.00");
  });

  it("renders a genuine zero-cost session as $0.00, not the not-reported glyph", async () => {
    getSessionMock.mockResolvedValueOnce(
      baseSession({
        total_cost_usd: 0,
        input_tokens: 0,
        output_tokens: 0,
        branches: [],
      }),
    );

    await mount();

    expect(container.textContent).toContain("$0.00");
  });
});
