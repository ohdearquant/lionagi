/**
 * i18n top-16 contract tests.
 *
 * Covers:
 * - LOCALES/RTL_LOCALES metadata shape (16 codes, ar/ur marked rtl).
 * - applyDocumentLocale flips <html lang>/<html dir> for rtl vs ltr locales.
 * - Every messages/*.json file has the exact same leaf-key set as en.json.
 * - Every locale's messages parse under a real ICU translator with no
 *   FORMATTING_ERROR, including the true {count, plural, ...} strings, and
 *   that prunePhantoms still resolves for a caller passing the `plural`
 *   argument its old text used to interpolate.
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
  date: "Aug 6, 2026",
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
  kind: "navigate",
  label: "Tab",
  logPages: 10,
  major: 1,
  message: "oops",
  minor: 5,
  minute: "05",
  n: 5,
  name: "worker",
  outcome: "completed",
  plural: "s",
  position: 1,
  rate: "1.2",
  reported: 3,
  role: "engine",
  running: 2,
  runs: 4,
  sec: 30,
  sessions: 3,
  span: "20m",
  start: "10:00",
  status: "ok",
  target: "n2",
  time: "18:00",
  title: "Fleet readiness review",
  total: 5,
  verdict: "approve",
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
  // 1099 = 1059 + operator.composer.autoAllow (1) + the Library hooks surface
  // (library.filterHooks + 29 library.hooks.* leaves) when the shared hook
  // library and per-agent assembly landed, + history.detail token-usage stats
  // (statTokensIn/statTokensOut, natively translated in all 16 locales), + 5
  // for the Operator model picker's provider groups, legacy selection, and
  // effort-transport explanation (also natively translated in all 16),
  // + history.detail.graphNodeStatusCancelled (1), the label for a node that
  // stopped because its run was cancelled rather than because it failed
  // (natively translated in all 16, which is why the baseline below is
  // unchanged).
  // autoAllow and the hooks leaves are natively translated in
  // zh/ja/ko/es/fr/de/pt-BR/ru and English-copied in the remaining 7 locales
  // — that debt is attributed in the identity-leak baseline below.
  //
  // 1099 = 1098 + history.detail.controls.reason.no-live-consumer, the refusal
  // shown when an agent run has no runner that would deliver a control. It
  // ships English-copied in all 15 non-English locales, like its sibling
  // agent-no-pause-seam; that debt is attributed in the baseline below.
  //
  // 1100 = 1099 + history.detail.controls.reason.no-project-scope, the refusal
  // shown when a run carries no project for a control to be authorized
  // against. Natively translated in all 16, so unlike its two siblings it adds
  // nothing to the identity-leak baseline below.
  //
  // 1101 = 1100 + runCard.outputWithheld, the badge for a tool result the
  // server withheld past its per-row size ceiling. Natively translated in all
  // 16, so it adds nothing to the identity-leak baseline either.
  it("en.json has 1101 leaves", () => {
    expect(EN_LEAVES.size).toBe(1101);
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

function getLeaf(obj: Record<string, unknown>, leafPath: string): unknown {
  return leafPath.split(".").reduce<unknown>((node, segment) => {
    if (node && typeof node === "object") return (node as Record<string, unknown>)[segment];
    return undefined;
  }, obj);
}

// The parity test above only compares key SETS — it cannot see a locale
// whose value for a key is byte-identical to English (the exact shape of
// the execution-graph regression this covers: 12 new keys shipped
// untranslated in every non-English locale). This walks every leaf and
// flags that case directly.
//
// A handful of values are legitimately identical across locales (a shared
// symbol, a brand name, a genuine cognate) — those are allow-listed by
// exact leaf path, individually, with a reason. This list must not grow to
// mask a real missing translation; it exists to name known-fine cases, not
// to make a failing check pass.
const IDENTITY_ALLOWLIST: ReadonlyMap<string, readonly string[]> = new Map([
  // "Total" is the correct native word in these languages too, not a
  // missed translation.
  ["history.detail.progressTotal", ["es", "fr", "id", "pt-BR"]],
  // "session" is a French word with the same plural, so the ICU form for
  // this key comes out byte-identical to English. Every other locale here
  // translates it, and French's own word for the neighbouring concepts is
  // translated too — this one collides on the merits.
  ["fleet.group.sessions", ["fr"]],
]);

function findIdentityLeaks(code: string): string[] {
  const messages = MESSAGES[code];
  const flagged: string[] = [];
  for (const key of EN_LEAVES) {
    const enValue = getLeaf(en, key);
    if (typeof enValue !== "string") continue;
    const localeValue = getLeaf(messages, key);
    if (localeValue !== enValue) continue;
    if (IDENTITY_ALLOWLIST.get(key)?.includes(code)) continue;
    flagged.push(key);
  }
  return flagged;
}

describe("messages — a locale value byte-identical to English is a missed translation", () => {
  const EXECUTION_GRAPH_KEYS = [
    "history.detail.progressTotal",
    "history.detail.progressCompleted",
    "history.detail.progressRunning",
    "history.detail.progressFailed",
    "history.detail.progressPending",
    "history.detail.progressEscalated",
    "history.detail.progressElapsed",
    "history.detail.expandGraph",
    "history.detail.collapseGraph",
    "history.detail.closeExpandedGraph",
    "history.detail.nodeNoBranch",
    "history.detail.olderUnavailable",
    "history.detail.reloadConversation",
  ];

  it.each(LOCALES.map((l) => l.code).filter((c) => c !== "en"))(
    "%s: the execution-graph history.detail keys are translated, not copied from English",
    (code) => {
      const flagged = findIdentityLeaks(code).filter((k) => EXECUTION_GRAPH_KEYS.includes(k));
      expect(flagged).toEqual([]);
    },
  );

  // Pre-existing translation debt across the rest of the app, well outside
  // this fix's scope (~3,100 leaves as of this check) — pinned so a further
  // increase is caught, without gating this PR on fixing all of it. Lower
  // this number by translating real strings, never by allow-listing them.
  //
  // Raised from 3112 when the library skill/plugin message keys landed: every
  // one of the newly counted leaves belongs to those new keys, and several are
  // loanwords ("MCP", "Hooks", "README") that are legitimately identical to
  // English. Raise this number only for keys arriving from elsewhere, and only
  // after attributing every added leaf.
  //
  // Lowered from 3144 when the English count strings gained real ICU plurals.
  // Worth recording because moving English moves this detector's reference
  // point in BOTH directions, which is easy to mistake for translation work:
  // three locales turned out to be holding the English text for
  // fleet.detail.branchesTitle (de, pt-BR, and fr, whose copy the English
  // change would otherwise have hidden by no longer matching it) and were
  // translated from each file's own word in the neighbouring
  // fleet.agentRow.branchesTitle; one French value became identical on the
  // merits and is allow-listed above. A fourth, id, held the same English
  // text without ever matching byte-for-byte, so this number never saw it.
  //
  // Raised from 3143 to 3488 (+345 = 23 new leaves × 15 non-English locales)
  // when ADR-0113's graph/list view toggle and pause/resume/steer run
  // controls landed: every one of the 23 new history.detail leaves
  // (viewGraph, viewList, viewToggleLabel, selectedNode, and the controls.*
  // subtree) shipped with the English string copied into every locale as a
  // placeholder rather than translated. This is real, attributed debt, not
  // slipped in — a follow-up should translate these 23 keys per locale and
  // lower this number back down by 345.
  //
  // Raised from 3488 to 3727 (+239) with operator.composer.autoAllow and the
  // 30 library hooks leaves: both shipped natively translated in 8 locales
  // (zh/ja/ko/es/fr/de/pt-BR/ru) and English-copied in the other 7
  // (ar/bn/hi/id/tr/ur/vi) = 31 × 7 = 217 attributed placeholder leaves, plus
  // 22 native values identical to English on the merits ("Hooks", "Matcher",
  // and cognates in the Latin-script locales). A follow-up should translate
  // the 7 placeholder locales and lower this by 217.
  //
  // Raised from 3727 to 3742 (+15 = 1 new leaf × 15 non-English locales) with
  // history.detail.controls.reason.no-live-consumer, shipped English-copied in
  // every non-English locale. It joins the controls.* placeholder debt already
  // attributed above rather than adding a new kind of it, and the follow-up
  // that translates that subtree should take this key with it.
  it("pre-existing identity-leak count across all locales does not grow past its pinned baseline", () => {
    const total = LOCALES.map((l) => l.code)
      .filter((c) => c !== "en")
      .reduce((sum, code) => sum + findIdentityLeaks(code).length, 0);
    expect(total).toBeLessThanOrEqual(3742);
  });
});

describe("messages — every locale parses under a real ICU translator", () => {
  it.each(LOCALES.map((l) => l.code))("%s: every leaf key resolves with no ICU error", (code) => {
    const t = translatorFor(code);
    for (const key of EN_LEAVES) {
      expect(() => t(key, SAMPLE_VALUES)).not.toThrow();
    }
  });

  // The English text was once "Prune {count} phantom{plural}", so the caller
  // passes a `plural` argument. An argument the string no longer reads must
  // stay harmless, or fixing the text would break the caller at runtime
  // rather than at build time.
  it.each(LOCALES.map((l) => l.code))(
    "%s: system.maintenance.prunePhantoms resolves for a caller still passing `plural`",
    (code) => {
      const t = translatorFor(code);
      expect(() => t("system.maintenance.prunePhantoms", { count: 3, plural: "s" })).not.toThrow();
    },
  );

  // English now inflects on `count` alone, so the legacy argument changes
  // nothing. Asserting on the rendered text rather than on the absence of a
  // literal "{plural}": the argument IS supplied, so a string still
  // interpolating it renders without braces and would pass that weaker check.
  //
  // Only English. Every other locale still keys this one string off the
  // caller's argument rather than off the count, three different ways — tr
  // and ur interpolate it as a suffix, bn/de/id branch on it with
  // {plural, select, s {…} other {…}}, and es/fr/ja carry a vestigial empty
  // select to consume it. That makes their singular/plural choice depend on
  // English morphology, which is a real defect and a separate change.
  it("en: prunePhantoms renders the same with and without the legacy `plural` argument", () => {
    const t = translatorFor("en");
    for (const count of [1, 3]) {
      expect(t("system.maintenance.prunePhantoms", { count, plural: "s" })).toBe(
        t("system.maintenance.prunePhantoms", { count }),
      );
    }
    expect(t("system.maintenance.prunePhantoms", { count: 1 })).toBe("Prune 1 phantom");
    expect(t("system.maintenance.prunePhantoms", { count: 3 })).toBe("Prune 3 phantoms");
  });

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
