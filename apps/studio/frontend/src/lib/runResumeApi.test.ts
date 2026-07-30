import { afterEach, describe, expect, it, vi } from "vitest";
import { resumeRun } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("resumeRun API", () => {
  it("posts the follow-up and selected branch to the run resume endpoint", async () => {
    let url = "";
    let init: RequestInit | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: string, request?: RequestInit) => {
        url = input;
        init = request;
        return Promise.resolve(
          new Response(
            JSON.stringify({
              run_id: "run-1",
              branch_id: "branch-1",
              invocation_id: "invocation-1",
            }),
            { status: 202, headers: { "content-type": "application/json" } },
          ),
        );
      }),
    );

    const result = await resumeRun("run-1", {
      instruction: "Continue and verify",
      branch_id: "branch-1",
    });

    expect(url).toContain("/api/runs/run-1/resume");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({
      instruction: "Continue and verify",
      branch_id: "branch-1",
    });
    expect(result.invocation_id).toBe("invocation-1");
  });
});
