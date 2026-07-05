/**
 * shell/ contract tests — khive.ai top-bar link and ecosystem footer line.
 *
 * Covers:
 * - TopBar.tsx exists, is wired into AppShell, and renders a new-tab
 *   khive.ai link with the correct href/target/rel
 * - StatusFooter.tsx renders the ecosystem note with a matching link
 */

import { describe, it, expect } from "vitest";
import * as fs from "node:fs";
import * as path from "node:path";

const SHELL_DIR = path.resolve(__dirname);

function read(file: string): string {
  return fs.readFileSync(path.join(SHELL_DIR, file), "utf-8");
}

describe("TopBar.tsx — new-tab khive.ai link", () => {
  it("exists", () => {
    expect(fs.existsSync(path.join(SHELL_DIR, "TopBar.tsx"))).toBe(true);
  });

  const src = read("TopBar.tsx");

  it("links to https://khive.ai", () => {
    expect(src).toMatch(/href="https:\/\/khive\.ai"/);
  });

  it("opens in a new tab", () => {
    expect(src).toMatch(/target="_blank"/);
  });

  it("guards the new tab with rel=noopener noreferrer", () => {
    expect(src).toMatch(/rel="noopener noreferrer"/);
  });

  it("is styled as a quiet link (muted foreground, brightens on hover)", () => {
    expect(src).toMatch(/text-content-muted/);
    expect(src).toMatch(/hover:text-content-primary/);
  });
});

describe("AppShell.tsx — TopBar wiring", () => {
  const src = read("AppShell.tsx");

  it("imports TopBar", () => {
    expect(src).toMatch(/import TopBar from ".\/TopBar"/);
  });

  it("renders <TopBar", () => {
    expect(src).toMatch(/<TopBar/);
  });

  it("keeps IconRail (logo/home behavior untouched)", () => {
    expect(src).toMatch(/<IconRail/);
  });
});

describe("StatusFooter.tsx — khive.ai ecosystem note", () => {
  const src = read("StatusFooter.tsx");

  it("links to https://khive.ai", () => {
    expect(src).toMatch(/href="https:\/\/khive\.ai"/);
  });

  it("opens in a new tab with rel=noopener noreferrer", () => {
    expect(src).toMatch(/target="_blank"/);
    expect(src).toMatch(/rel="noopener noreferrer"/);
  });

  it("renders the ecosystem prefix/suffix copy via i18n", () => {
    expect(src).toMatch(/footer\.ecosystemPrefix/);
    expect(src).toMatch(/footer\.ecosystemLink/);
    expect(src).toMatch(/footer\.ecosystemSuffix/);
  });
});

describe("messages — topbar + footer.ecosystem keys present in both locales", () => {
  const MESSAGES_DIR = path.resolve(SHELL_DIR, "../../messages");

  for (const locale of ["en", "zh"]) {
    it(`${locale}.json has shell.topbar.khiveLink and shell.footer.ecosystem*`, () => {
      const messages = JSON.parse(
        fs.readFileSync(path.join(MESSAGES_DIR, `${locale}.json`), "utf-8"),
      );
      expect(messages.shell.topbar.khiveLink).toBe("khive.ai");
      expect(messages.shell.footer.ecosystemLink).toBe("khive.ai");
      expect(typeof messages.shell.footer.ecosystemPrefix).toBe("string");
      expect(typeof messages.shell.footer.ecosystemSuffix).toBe("string");
    });
  }
});
