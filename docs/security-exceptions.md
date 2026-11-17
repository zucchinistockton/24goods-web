# Security exceptions

Every exception to a blocking security check is listed here with its scope, risk assessment, owner
approval and a **mandatory review date**. An exception covers exactly one advisory. All other
findings of the same check stay blocking. `frontend/scripts/lint.mjs` (run by `pnpm lint` in CI)
fails once a review date has passed, so an exception cannot silently outlive its review.

## GHSA-fc5q-39xc-vjpf: picomatch ReDoS (pnpm audit, high)

| | |
|---|---|
| Advisory | [GHSA-fc5q-39xc-vjpf](https://github.com/advisories/GHSA-fc5q-39xc-vjpf), "picomatch vulnerable to ReDoS through single-branch repeated extglobs", severity high |
| Affected | `picomatch` <= 4.0.7. **No patched release exists** (checked 2026-11-17; 4.0.7 is the latest) |
| Check | CI `pnpm audit --prod --audit-level high` (frontend job), blocking |
| Mechanism | `auditConfig.ignoreGhsas` in `frontend/pnpm-workspace.yaml` (pnpm 12.15.0). It ignores this GHSA id only: other advisories, other packages and new advisories against picomatch still fail the job |
| Approved | Owner, 2026-11-17: temporary and narrowly scoped, subject to the verification below |
| **Mandatory review** | **2026-12-17**, or earlier as soon as a patched `picomatch` or a `vue-router` that drops it is released. Then remove the exception and upgrade |

### Risk assessment (verified 2026-11-17 against the lockfile, `node_modules` and the deployed demo build)

1. **How it is reached.** `pnpm why picomatch --prod -r`: the only production path is `vue-router@5.4.0`,
   either directly or through its build-time helpers (`tinyglobby`, `fdir`, `unplugin`, `unplugin-utils`,
   `@vue-macros/common`). Both apps (`@24goods/web`, `@24goods/admin`) depend on `vue-router`.
2. **Where it runs inside vue-router.** Only the file-based-routing plugin chunk (`dist/unplugin-*.mjs` and
   `.cjs`) imports `picomatch`. Only the plugin entry points `vue-router/vite`, `/unplugin`, `/rollup`,
   `/rolldown`, `/webpack` and `/esbuild` load that chunk. The runtime entry
   (`dist/vue-router.esm-bundler.js`) imports none of these packages.
3. **What we load.** Our sources import only the main `vue-router` entry (46 imports, no subpath), and no
   Vite config uses a vue-router plugin. The plugin chunk, and with it `picomatch`, is therefore **never loaded,
   not even at build time**.
4. **Deployed runtime.** The web image (Caddy serving static files) has no Node.js binary and no
   `node_modules`. Its 183 shipped JS files (buyer app, console, redemption page) contain no `picomatch`,
   `tinyglobby` or `unplugin` code. The backend is Python and does not use it.
5. **Untrusted input.** The vulnerable function compiles glob patterns. Even in a project that used the plugin,
   its input would be route-file globs written by developers at build time, never user data. In this project
   it is not called at all.

**Conclusion:** not reachable from untrusted input and absent from deployed runtime code. Residual risk:
none at runtime. At build time it is unused today.

### Conditions that void the exception

- Any import of a vue-router build-plugin entry (see 2). `pnpm lint` refuses such an import.
- `picomatch` reaching production through another package: the ignore would hide it, so re-run
  `pnpm why picomatch --prod -r` at every dependency update touching `vue-router`, `vite` or `unplugin`, and at
  the review.
- Any use of `picomatch` (directly or through a helper) on data from requests, uploads or the database.

### Review procedure (2026-12-17 or earlier)

1. `pnpm view picomatch version` and the advisory: is a patched version out? If yes, update the lockfile
   (Dependabot or a pnpm override to the fixed version), delete the `ignoreGhsas` entry, and remove this
   section.
2. If not: repeat checks 1-4 above. Extend only with a new owner approval and a new date of at most 30 days.
