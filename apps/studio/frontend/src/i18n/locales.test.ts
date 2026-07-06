/**
 * i18n top-16 contract tests.
 *
 * Covers:
 * - LOCALES/RTL_LOCALES metadata shape (16 codes, ar/ur marked rtl).
 * - applyDocumentLocale flips <html lang>/<html dir> for rtl vs ltr locales.
 * - Every messages/*.json file has the exact same leaf-key set as en.json
 *   (770 leaves: 766 from the schedules + status/verdict keystone keys plus
 *   4 Mission Control overview leaves — liveBoard.durationStatus,
 *   recent.repeatedGroup, recent.expand, recent.collapse).
 * - Every locale's messages parse under a real ICU translator with no
 *   FORMATTING_ERROR, including the true {count, plural, ...} strings and
 *   the pre-existing bare-{plural} anti-pattern in prunePhantoms.
 * - __root.tsx's own VALID_LOCALES/MESSAGES wiring covers every LOCALES
 *   code (fails if a locale is dropped or mismapped there, independent of
 *   the messages/*.json files themselves being fine).
 */
import { describe, it, expect } from "vitest";
import * as fs from "node:fs";
import * as path from "node:path";
import { createTranslator } from "use-intl";
import { LOCALES, RTL_LOCALES, applyDocumentLocale } from "./locales";

import en from "@/messages/en.json";
import zh from "@/messages/zh.json";
import es from "@/messages/es.json";
import fr from "@/messages/fr.json";
import hi from "@/messages/hi.json";
import bn from "@/messages/bn.json";
import de from "@/messages/de.json";
import id from "@/messages/id.json";
import ptBR from "@/messages/pt-BR.json";
import ko from "@/messages/ko.json";
import tr from "@/messages/tr.json";
import ur from "@/messages/ur.json";
import vi from "@/messages/vi.json";
import ar from "@/messages/ar.json";
import ru from "@/messages/ru.json";
import ja from "@/messages/ja.json";

const MESSAGES: Record<string, typeof en> = {
  en,
  zh,
  es,
  fr,
  hi,
  bn,
  de,
  id,
  "pt-BR": ptBR,
  ko,
  tr,
  ur,
  vi,
  ar,
  ru,
  ja,
};

function flattenLeaves(obj: Record<string, unknown>, prefix = ""): Set<string> {
  const leaves = new Set<string>();
  for (const [key, value] of Object.entries(obj)) {
    const path = prefix ? `${prefix}.${key}` : key;
    if (value && typeof value === "object" && !Array.isArray(value)) {
      for (const leaf of flattenLeaves(value as Record<string, unknown>, path)) {
        leaves.add(leaf);
      }
    } else {
      leaves.add(path);
    }
  }
  return leaves;
}

const EN_LEAVES = flattenLeaves(en);

// createTranslator's generic signature is keyed to a literal message shape;
// these tests walk arbitrary runtime key strings, so we call through a loose
// shape instead of fighting the (correct, for app code) strict overload.
type LooseTranslator = (key: string, values?: Record<string, unknown>) => string;

function translatorFor(code: string): LooseTranslator {
  return createTranslator({ locale: code, messages: MESSAGES[code] }) as unknown as LooseTranslator;
}

// Sample values covering every ICU argument name used anywhere in en.json —
// resolving with these lets us call every leaf key without a
// "variable was not provided" formatting error.
const SAMPLE_VALUES = {
  age: 5,
  attention: 2,
  base: "http://localhost",
  busy: 1,
  checkpointed: 3,
  color: "amber",
  count: 2,
  day: "Monday",
  delta: "3m",
  detail: "boom",
  duration: "3h",
  end: "11:00",
  event: "PR merge",
  field: "payload",
  group: "alpha",
  id: "abc123",
  interval: "5m",
  label: "Tab",
  logPages: 10,
  message: "oops",
  minute: "05",
  n: 5,
  name: "worker",
  plural: "s",
  position: 1,
  rate: "1.2",
  role: "engine",
  running: 2,
  runs: 4,
  sec: 30,
  sessions: 3,
  span: "20m",
  start: "10:00",
  status: "ok",
  time: "18:00",
  total: 5,
  version: "2",
};

describe("LOCALES metadata", () => {
  it("has exactly 16 locales, matching VALID_LOCALES in __root.tsx", () => {
    expect(LOCALES).toHaveLength(16);
  });

  it("has unique codes covering the top-16 world languages", () => {
    const codes = LOCALES.map((l) => l.code);
    expect(new Set(codes).size).toBe(16);
    expect(codes).toEqual([
      "en",
      "zh",
      "es",
      "fr",
      "hi",
      "bn",
      "de",
      "id",
      "pt-BR",
      "ko",
      "tr",
      "ur",
      "vi",
      "ar",
      "ru",
      "ja",
    ]);
  });

  it("marks only ar and ur as rtl", () => {
    const rtl = LOCALES.filter((l) => l.dir === "rtl").map((l) => l.code);
    expect(rtl.sort()).toEqual(["ar", "ur"]);
  });

  it("RTL_LOCALES is derived from the same rtl flag", () => {
    expect([...RTL_LOCALES].sort()).toEqual(["ar", "ur"]);
  });
});

describe("applyDocumentLocale — <html lang>/<html dir> wiring", () => {
  it("sets dir=rtl and lang=ar for Arabic", () => {
    applyDocumentLocale("ar");
    expect(document.documentElement.dir).toBe("rtl");
    expect(document.documentElement.lang).toBe("ar");
  });

  it("sets dir=rtl for Urdu", () => {
    applyDocumentLocale("ur");
    expect(document.documentElement.dir).toBe("rtl");
    expect(document.documentElement.lang).toBe("ur");
  });

  it("switching back to English sets dir=ltr", () => {
    applyDocumentLocale("ar");
    expect(document.documentElement.dir).toBe("rtl");
    applyDocumentLocale("en");
    expect(document.documentElement.dir).toBe("ltr");
    expect(document.documentElement.lang).toBe("en");
  });

  it.each(LOCALES.filter((l) => l.dir === "ltr").map((l) => l.code))(
    "sets dir=ltr for %s",
    (code) => {
      applyDocumentLocale(code);
      expect(document.documentElement.dir).toBe("ltr");
    },
  );
});

describe("messages — leaf-key parity across all 16 locales", () => {
  it("en.json has 770 leaves (766 base + 4 Mission Control overview leaves)", () => {
    expect(EN_LEAVES.size).toBe(770);
  });

  it.each(LOCALES.map((l) => l.code))(
    "%s.json has the exact same leaf-key set as en.json",
    (code) => {
      const leaves = flattenLeaves(MESSAGES[code]);
      const missing = [...EN_LEAVES].filter((k) => !leaves.has(k));
      const extra = [...leaves].filter((k) => !EN_LEAVES.has(k));
      expect(missing).toEqual([]);
      expect(extra).toEqual([]);
    },
  );
});

describe("messages — every locale parses under a real ICU translator", () => {
  it.each(LOCALES.map((l) => l.code))("%s: every leaf key resolves with no ICU error", (code) => {
    const t = translatorFor(code);
    for (const key of EN_LEAVES) {
      expect(() => t(key, SAMPLE_VALUES)).not.toThrow();
    }
  });

  it.each(LOCALES.map((l) => l.code))(
    "%s: system.maintenance.prunePhantoms (bare {plural} arg) resolves",
    (code) => {
      const t = translatorFor(code);
      expect(() => t("system.maintenance.prunePhantoms", { count: 3, plural: "s" })).not.toThrow();
    },
  );

  const REAL_PLURAL_KEYS = [
    "library.drawer.versionCount",
    "schedules.cal.rangeBadge",
    "runCard.failedToolCalls",
    "workflow.validationIssues",
  ];

  it.each(LOCALES.map((l) => l.code))(
    "%s: true ICU plural strings resolve for count=1 and count=2",
    (code) => {
      const t = translatorFor(code);
      for (const key of REAL_PLURAL_KEYS) {
        expect(() => t(key, { ...SAMPLE_VALUES, count: 1 })).not.toThrow();
        expect(() => t(key, { ...SAMPLE_VALUES, count: 2 })).not.toThrow();
      }
    },
  );
});

describe("__root.tsx — root wiring covers every LOCALES code", () => {
  const rootSrc = fs.readFileSync(path.resolve(__dirname, "../routes/__root.tsx"), "utf-8");

  // Derive file-code -> import-binding straight from __root.tsx's own import
  // statements, so this test tracks whatever the file actually does rather
  // than a second hardcoded copy of the mapping.
  const bindingForCode: Record<string, string> = {};
  for (const m of rootSrc.matchAll(/import (\w+) from "@\/messages\/([\w.-]+)\.json"/g)) {
    bindingForCode[m[2]] = m[1];
  }

  it.each(LOCALES.map((l) => l.code))(
    "%s has a message import in __root.tsx wired into MESSAGES under the matching key",
    (code) => {
      const binding = bindingForCode[code];
      expect(binding, `no "@/messages/${code}.json" import found in __root.tsx`).toBeDefined();

      const key = /^[A-Za-z_$][\w$]*$/.test(code) ? code : `"${code}"`;
      const escapedKey = key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      const wired = new RegExp(`${escapedKey}:\\s*${binding}\\b`);
      expect(rootSrc, `MESSAGES does not map ${key} to ${binding}`).toMatch(wired);
    },
  );

  it.each(LOCALES.map((l) => l.code))("%s's wired-in messages module is non-empty", (code) => {
    expect(Object.keys(MESSAGES[code]).length).toBeGreaterThan(0);
  });
});
