# jsx-a11y ESLint Performance Baseline (#1135)

Date: 2026-06-03

## Methodology

- Worktree: `show/lionagi-sweep/studio-frontend`
- Directory: `apps/studio/frontend`
- Runtime: Node `v25.6.0`, pnpm `10.15.0`
- Commands:
  - `pnpm lint` (`eslint .`)
  - `pnpm build` (`next build`)
- Timing method: `/usr/bin/time -p`, reporting wall-clock `real` seconds.
- Repetitions: `n=3` per condition.
- Build method: removed `.next` before each build; reused installed dependencies and toolchain caches.
- Bundle-size metric: summed bytes for `.next/static` JavaScript and CSS files after each successful build.
- jsx-a11y enabled condition: shipped `eslint.config.mjs`.
- jsx-a11y disabled condition: temporary config removed the real `eslint-plugin-jsx-a11y` import and stripped active `jsx-a11y/*` rules from `eslint-config-next`. An inert local namespace was added only so existing `eslint-disable` comments for jsx-a11y rule IDs would parse; `eslint --print-config app/runs/page.tsx` showed no active `jsx-a11y/*` rules.
- Restoration check: `eslint.config.mjs` SHA-256 before and after measurement was `0bc03ee3ef328ccf35124318873816b2351c7ee94e4cd54ac55c388c5dff1ea5`.

## Lint Time

| Condition         |    Runs, seconds | Mean | Stddev |    CV | Result                      |
| ----------------- | ---------------: | ---: | -----: | ----: | --------------------------- |
| jsx-a11y enabled  | 4.15, 6.33, 5.00 | 5.16 |   1.10 | 21.3% | pass, 0 errors / 3 warnings |
| jsx-a11y disabled | 3.90, 3.97, 3.94 | 3.94 |   0.04 |  0.9% | pass, 0 errors / 7 warnings |

Observed lint delta: enabled mean minus disabled mean = `+1.22s`. The enabled lint sample is noisy (`CV=21.3%`), so treat this as a local baseline rather than a statistically stable estimate.

The disabled condition reports more warnings because existing `eslint-disable` comments for jsx-a11y rules become unused when those rules are inactive.

## Build Time

| Condition                   |       Runs, seconds |  Mean | Stddev |    CV | Result |
| --------------------------- | ------------------: | ----: | -----: | ----: | ------ |
| jsx-a11y enabled            | 13.80, 13.22, 21.13 | 16.05 |   4.41 | 27.5% | pass   |
| jsx-a11y disabled           |    5.71, 5.65, 5.90 |  5.75 |   0.13 |  2.3% | pass   |
| post-restore enabled sanity |                6.09 |  6.09 |    n/a |   n/a | pass   |

The first enabled build group shows warmup/cache noise (`CV=27.5%`). Because `next build` does not execute the ESLint rule set here, the post-restore enabled sanity build is the better check for whether jsx-a11y affects build execution. It remained comparable to the disabled build timings.

## Production Bundle Size

| Condition                   | Static JS/CSS files | Static JS/CSS bytes | Delta vs enabled |
| --------------------------- | ------------------: | ------------------: | ---------------: |
| jsx-a11y enabled            |                  45 |           1,482,391 |                0 |
| jsx-a11y disabled           |                  45 |           1,482,391 |                0 |
| post-restore enabled sanity |                  45 |           1,482,391 |                0 |

## Conclusion

`eslint-plugin-jsx-a11y` has zero production-bundle impact in this frontend: the built `.next/static` JavaScript/CSS payload is byte-identical with and without active jsx-a11y ESLint rules.

The only measured effect is on `pnpm lint`, where the local three-run mean was `5.16s` with jsx-a11y enabled versus `3.94s` without active jsx-a11y rules. Build-time differences are attributable to build cache/warmup variance, not the ESLint plugin.

Config restoration: confirmed. `eslint.config.mjs` was restored to the original SHA-256 hash and has no git diff after measurement.
