/**
 * RunDetail — backward message-cursor pagination.
 *
 * The server windows a branch's messages from the tail and returns a stable
 * per-branch anchor cursor for "load older" (services/sessions.py
 * _window_message_ids / _encode_message_cursor). The discriminating scenario
 * this file covers: a "load older" fetch lands *after* new messages have
 * already landed at the tail of the same branch. An offset-based "load
 * older" (the code this replaces) would shift under that tail growth and
 * either skip or repeat rows; a cursor-based one must not, because the
 * anchor is a message id, not a position.
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
  getSessionStatistics: vi.fn().mockRejectedValue(new Error("statistics unavailable in test")),
  getInvocation: vi.fn().mockRejectedValue(new Error("no invocation in this test")),
  streamSession: vi.fn(() => () => {}),
  streamSignals: vi.fn(() => () => {}),
  resumeRun: vi.fn(),
  getResumeAvailability: vi.fn().mockResolvedValue({
    run_id: "test",
    invocation_kind: "agent",
    resumable: true,
  }),
}));

const { default: RunDetail } = await import("./RunDetail");

function msg(id: string, text: string, ts: number) {
  return {
    id,
    role: "user",
    content: { instruction: text },
    sender: "operator",
    timestamp: ts,
    lion_class: "Instruction",
  };
}

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

describe("RunDetail — cursor-based backward pagination", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    getSessionMock.mockReset();
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    // jsdom does not implement scrollIntoView; RunDetail calls it on load.
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

  function findButton(text: string): HTMLButtonElement | undefined {
    return [...container.querySelectorAll("button")].find((b) => b.textContent?.includes(text)) as
      | HTMLButtonElement
      | undefined;
  }

  it("pages older messages by cursor (not offset), merging with no skip and no duplicate even as the tail grows between fetches", async () => {
    // Initial page: newest 3 of 5 messages, with a next-page anchor.
    getSessionMock.mockResolvedValueOnce(
      baseSession({
        message_next_cursor: "cursor-1",
        branches: [
          {
            id: "b1",
            name: "main",
            created_at: 1,
            message_total: 5,
            message_has_older: true,
            messages: [msg("m3", "three", 3), msg("m4", "four", 4), msg("m5", "five", 5)],
          },
        ],
      }),
    );

    await mount();

    expect(getSessionMock).toHaveBeenCalledTimes(1);
    expect(getSessionMock).toHaveBeenCalledWith("s1");

    // RunStepCard's own "N/total" counter reflects branch.messages.length —
    // the merge-correctness signal: a skip undercounts, a duplicate
    // overcounts, only an exact match proves neither happened.
    expect(container.textContent).toContain("/3");

    const loadOlder = findButton("Load older messages");
    expect(
      loadOlder,
      "load-older control must be visible when a branch has hidden history",
    ).not.toBeUndefined();

    // Between the initial load and this fetch, two more messages (m6, m7)
    // landed at the tail server-side — message_total grows accordingly. The
    // older page must still resolve relative to the m3 anchor, unaffected by
    // that tail growth.
    getSessionMock.mockResolvedValueOnce(
      baseSession({
        message_next_cursor: null,
        branches: [
          {
            id: "b1",
            name: "main",
            created_at: 1,
            message_total: 7,
            message_has_older: false,
            messages: [msg("m1", "one", 1), msg("m2", "two", 2)],
          },
        ],
      }),
    );

    await act(async () => {
      loadOlder?.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(getSessionMock).toHaveBeenCalledTimes(2);
    // The second call must carry the anchor cursor, never an offset — an
    // offset here is exactly the bug this pagination replaces.
    expect(getSessionMock).toHaveBeenNthCalledWith(2, "s1", { messageCursor: "cursor-1" });

    // Merged branch now carries exactly 5 messages (3 initial + 2 older, 0
    // overlap) — not 4 (a skip) and not 6+ (a duplicate). The newly-landed
    // tail messages (server total went to 7) are correctly excluded: neither
    // fetched window ever included them.
    expect(container.textContent).toContain("/5");
    expect(container.textContent).not.toContain("/4");
    expect(container.textContent).not.toContain("/6");
    expect(container.textContent).not.toContain("/7");
  });

  it("retires the load-older control when the last page arrives, even though tail growth leaves messages unloaded", async () => {
    // Same shape as the merge test above, isolated on what the control says
    // afterwards. The final older page carries message_next_cursor: null, so
    // there is no older history left to ask for, while message_total has
    // grown to 7 against 5 loaded messages because two newer ones landed at
    // the tail. Counting those two as older history renders a control that
    // is enabled and returns without doing anything, every time it is
    // pressed, for the rest of the session.
    getSessionMock.mockResolvedValueOnce(
      baseSession({
        message_next_cursor: "cursor-1",
        branches: [
          {
            id: "b1",
            name: "main",
            created_at: 1,
            message_total: 5,
            message_has_older: true,
            messages: [msg("m3", "three", 3), msg("m4", "four", 4), msg("m5", "five", 5)],
          },
        ],
      }),
    );
    await mount();

    const loadOlder = findButton("Load older messages");
    expect(loadOlder).not.toBeUndefined();

    getSessionMock.mockResolvedValueOnce(
      baseSession({
        message_next_cursor: null,
        branches: [
          {
            id: "b1",
            name: "main",
            created_at: 1,
            message_total: 7,
            message_has_older: false,
            messages: [msg("m1", "one", 1), msg("m2", "two", 2)],
          },
        ],
      }),
    );
    await act(async () => {
      loadOlder?.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    // The unloaded count is still positive here (7 total, 5 loaded), which is
    // what made the old arithmetic keep the control alive.
    expect(container.textContent).toContain("/5");
    expect(
      findButton("Load older messages"),
      "no cursor means nothing older to fetch, so the control must not be offered",
    ).toBeUndefined();
    expect(container.textContent).not.toContain("older messages");
  });

  it("asks for a cursor page once when the scroll sentinel reports twice in the same turn", async () => {
    // The sentinel above the message list fires whenever it scrolls into view,
    // and two intersections can be delivered in a single turn. React state
    // does not update between them, so an in-flight test that reads render
    // state sees both callbacks as idle and sends the same cursor twice.
    let notify: (() => void) | undefined;
    class TwoShotObserver {
      constructor(cb: (entries: { isIntersecting: boolean }[]) => void) {
        notify = () => cb([{ isIntersecting: true }]);
      }
      observe() {}
      disconnect() {}
      unobserve() {}
    }
    vi.stubGlobal("IntersectionObserver", TwoShotObserver);

    getSessionMock.mockResolvedValueOnce(
      baseSession({
        message_next_cursor: "cursor-1",
        branches: [
          {
            id: "b1",
            name: "main",
            created_at: 1,
            message_total: 5,
            message_has_older: true,
            messages: [msg("m3", "three", 3), msg("m4", "four", 4), msg("m5", "five", 5)],
          },
        ],
      }),
    );
    await mount();
    expect(getSessionMock).toHaveBeenCalledTimes(1);

    // The page never resolves during the two intersections, so the only thing
    // that can suppress the second request is a synchronous guard.
    getSessionMock.mockReturnValueOnce(new Promise(() => {}));

    await act(async () => {
      notify?.();
      notify?.();
      await Promise.resolve();
    });

    expect(
      getSessionMock,
      "a second intersection in the same turn must not re-send the cursor already in flight",
    ).toHaveBeenCalledTimes(2);
    expect(getSessionMock).toHaveBeenNthCalledWith(2, "s1", { messageCursor: "cursor-1" });
  });

  it("shows a visible recovery, not a dead control, when the held anchor is rejected as stale", async () => {
    getSessionMock.mockResolvedValueOnce(
      baseSession({
        message_next_cursor: "cursor-1",
        branches: [
          {
            id: "b1",
            name: "main",
            created_at: 1,
            message_total: 5,
            message_has_older: true,
            messages: [msg("m3", "three", 3), msg("m4", "four", 4), msg("m5", "five", 5)],
          },
        ],
      }),
    );
    await mount();

    const loadOlder = findButton("Load older messages");
    getSessionMock.mockRejectedValueOnce(
      new MockApiError(400, "message_cursor anchor not found in branch progression"),
    );

    await act(async () => {
      loadOlder?.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(findButton("Load older messages")).toBeUndefined();
    expect(container.textContent).toContain("Older history could not be loaded");
    const reload = findButton("Reload conversation");
    expect(
      reload,
      "a reload affordance must replace the dead load-older button",
    ).not.toBeUndefined();

    // The reload action re-fetches from scratch (no cursor) and clears the
    // stuck state.
    getSessionMock.mockResolvedValueOnce(
      baseSession({
        message_next_cursor: "cursor-2",
        branches: [
          {
            id: "b1",
            name: "main",
            created_at: 1,
            message_total: 6,
            message_has_older: true,
            messages: [msg("m4", "four", 4), msg("m5", "five", 5), msg("m6", "six", 6)],
          },
        ],
      }),
    );
    await act(async () => {
      reload?.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(getSessionMock).toHaveBeenNthCalledWith(3, "s1");
    expect(container.textContent).not.toContain("Older history could not be loaded");
    expect(findButton("Load older messages")).not.toBeUndefined();
  });
});
