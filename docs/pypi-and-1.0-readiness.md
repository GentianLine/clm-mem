# Publishing to PyPI & the 1.0 quality bar

**Scope:** getting `shatterling` (the `clm-mem` package) onto PyPI, and defining
what "good enough" means before we call it **1.0**.

**The owner's bar (this document is built around it):**
> Don't publish until it is **secure enough to run as a developer** and
> **tested enough to feel like it'll probably work.**

That translates into two independent gates, below:

- **Gate S — Safe for a developer to run.** Someone who `pip install`s this and
  points it at their own machine must not be able to get RCE'd by the package's
  own defaults, and every opt-in danger (exec tools, non-loopback bind) must be
  *impossible to hit by accident* and *loud when hit on purpose*.
- **Gate T — Probably works.** A new user following the README happy path gets a
  working memory server; the core behavior (remember → evict → still recall) is
  covered by tests that actually run, and we can point at a coverage number.

**Recommendation up front:** the codebase is *close* to publishable as a **0.x**,
but it is **not ready for 1.0**. Two things block even a 0.x upload (the naming
decision + a stale version constant), and Gate S has one design-level item
(`jail` isolation) that must be made *unmissable* before we let a developer run
it with tools on. See the go/no-go checklist at the end.

---

## 0. The naming decision (blocks everything — decide first)

There are currently **three** names in play, and a published package needs them
to line up or users will be confused:

| Name | Where it lives today | Role |
|---|---|---|
| `clm-mem` | repo name, console script (deprecated alias), docs/brand | public brand |
| `purslane` | canonical console script (`pyproject [project.scripts]`) | the CLI you run |
| `shatterling` | Python package / import namespace, PyPI `name =` | what you `import` |

This is a **one-time, hard-to-reverse** choice (a taken PyPI project name and an
established import path are both sticky), so make it deliberately. Options:

- **Option A — keep the current split.** PyPI name `shatterling`, CLI `purslane`
  (+ `clm-mem` alias). *Pros:* no code churn. *Cons:* three names; a user who
  `pip install shatterling` then searches for "clm-mem" finds the repo, not the
  package. This is the most confusing option to publish.
- **Option B (recommended) — one public name, `clm-mem`.** PyPI distribution
  name **`clm-mem`**, console script **`clm-mem`**, import namespace stays
  `shatterling` (or also rename to `clm_mem`). *Pros:* the name people already
  use for the repo/CLI is the thing they install; one brand. The Python package
  can remain `shatterling` internally if you like the codename — only the
  distribution + entry point need to match the public name. *Cons:* must pick
  whether imports are `clm_mem.*` or `shatterling.*` and be consistent.
- **Option C — go fully `shatterling`.** PyPI + CLI + imports all `shatterling`;
  drop `clm-mem` as a brand. *Pros:* cleanest single name. *Cons:* abandons the
  existing repo/CLI brand; bigger rename blast radius (repo URL, docs, README).

**Decision needed:** which public name, and do imports follow it? Until this is
settled, do **not** build an sdist/wheel — you'd be minting a name you may want
back. (The 0.5 roadmap item "remove the deprecated `clm-mem` alias" is the same
decision in miniature.)

---

## 1. Packaging work to reach "publishable" (ordered)

These are the concrete, mostly-small tasks between *here* and a clean PyPI page.
None requires new features; all are correctness/hygiene.

1. **Fix the stale version constant.** `shatterling/purslane/server.py:50` has
   `VERSION = "0.3.0"` while `pyproject.toml`, `__init__.py`, and the changelog
   all say `0.4.0`. The `/version` endpoint and `/health` report the wrong
   version to every client. Make it read from one source (e.g. import
   `shatterling.__version__`) so it can't drift again. *User-visible bug — fix
   before any release.*
2. **Declare a `[build-system]` table.** `pyproject.toml` has none, so the build
   backend is implied. Add it explicitly so sdist/wheel builds are reproducible:
   ```toml
   [build-system]
   requires = ["setuptools>=61", "wheel"]
   build-backend = "setuptools.build_meta"
   ```
3. **Tighten metadata for a trustworthy PyPI page.**
   - `license`: prefer the SPDX form `license = "MIT"` (PEP 639) or keep
     `{ text = "MIT" }` — but fill in the LICENSE holder: it currently reads
     `Copyright (c) 2026` with **no name**. A published package should say who.
   - `classifiers`: only two today. Add the Python versions CI actually tests
     (`3.11 / 3.12 / 3.13`), `License :: OSI Approved :: MIT License`,
     `Operating System :: OS Independent`, and a couple of relevant topics.
     Classifiers are how PyPI shows trust + discoverability.
   - Add an `authors`/`maintainers` field (name + email) so the page isn't blank.
4. **Freeze dependencies honestly.** The only hard dep is `openai>=1.30`. That's
   fine, but confirm it's the *minimum* that works (test against a low bound in
   CI or pin a known-good floor). `requirements.lock.txt` is a dev snapshot, not
   the published constraint — keep them distinct and say so.
5. **Add a release workflow + build check to CI.** Today CI only runs test+lint.
   Add (a) a step that builds sdist+wheel on every push (`python -m build`) so a
   broken package is caught before release, and (b) a manual/dispatched "publish"
   job gated on green tests (see §6). Do **not** wire auto-publish to `main`.
6. **Decide the deprecated-alias timeline.** `clm-mem` console script is marked
   "remove in 0.5." If Option B makes `clm-mem` the *primary* name, that note
   flips — keep the two consistent so a future reader isn't confused.

**Not required for first publish (nice-to-have):** a `docs/` auto-publish to
ReadTheDocs, a `CHANGELOG` link in the README's PyPI-rendered section, and
`[project.urls]` (Repository / Issues) — cheap and worth adding while you're in
the file.

---

## 2. Gate S — "secure enough to run as a developer"

The security audit (`SECURITY_AUDIT.md`) is the source of truth. Status as of now:

**Already remediated (do not regress):** C-1 (non-loopback bind without an API
key **refuses to start**), C-3 + C-4 (SSRF + arbitrary-scheme web fetch), M-1
(CORS credentials off by default), M-2 (model/MCP-initiated tool calls go
through the approval broker), M-3 (canonical session-id validation at the router
edge), L-1 (request-body cap), L-2 (no inbound `Authorization` relayed upstream
by default), M-4 (unique temp names in `run_python`), M-5 (`/health` disclosure
reduced when auth is on), **L-5 (per-client rate limiting — added this cycle)**.

**What still has to be true for a developer to run it safely:**

- **[S1] Make the `jail` isolation limitation unmissable (audit C-2 — the big one).**
  Default when tools are enabled is `sandbox_mode = "jail"`, which is a *limited
  process, not real isolation* — an agent in jail mode can effectively run
  arbitrary commands on the host. Tools are **off by default** (`tools_enabled:
  False`), so the safe path is the default; that's good and must stay. But when a
  developer opts in with `--tools`, the docs, the serve banner, **and** the web
  UI must say, plainly: *jail = host-level exec; use `--sandbox-mode docker` for
  real isolation.* Right now this is documented but understated in the UI. This
  is the single highest-leverage "safe to run as a developer" item.
- **[S2] Surface the Docker-fallback downgrade loudly (audit L-4).** When
  `--sandbox-mode docker` is requested but the docker CLI is missing, it falls
  back to `jail` with an explanatory note in `ExecResult.note`. Confirm that note
  reaches the user (banner + UI), not just a field nobody reads. A silent
  downgrade from "isolated" to "host exec" is exactly the kind of thing that
  should scare a developer, not hide.
- **[S3] Per-route capability split (roadmap; audit C-1 follow-through).** A
  memory-only deployment should be able to expose **no** exec/file/web-fetch
  routes at all. `api_only` mode (M7) removes native passthrough + studio but a
  full `serve` with tools still exposes the agent surface. Add a way to run the
  proxy with *only* the CLM session verbs and no tool routes, so "I just want
  memory" doesn't ship an exec endpoint by default. (Partially addressed by
  `tools_enabled: False`; this is about the *route surface*, not the toggle.)
- **[S4] Re-run the audit after M2/M6 land.** The new dispatch path and any
  streaming code are exactly where regressions hide (already on the roadmap).
  For 1.0, a fresh pass over the security-critical paths — auth, SSRF guard,
  sandbox confinement, approval broker, rate limiter — with the results recorded,
  is part of the bar, not optional.

**Definition of done for Gate S:** a developer who installs and runs the package
with its defaults cannot be RCE'd; every opt-in (tools, non-loopback) either
refuses to start or prints a warning they would actually read; and there is a
written "if you enable tools, here's what can happen on your machine" section in
the README that matches what the code does.

---

## 3. Gate T — "tested enough to feel like it'll probably work"

Current state: **171 tests, ~80s, all offline/mock** (no network, no real model).
That's a healthy unit/integration base, but three gaps stand between it and
"probably works":

- **[T1] Measure coverage and set a floor.** There is no `pytest-cov` and no
  coverage number today — "tested enough" can't be claimed without a number.
  Add `pytest-cov`, report line coverage, and enforce a minimum in CI. A
  reasonable 1.0 floor: **≥ 85% line coverage on `shatterling/engine/` and
  `shatterling/purslane/`**, with **security-critical paths (auth check, SSRF
  guard, sandbox path confinement, approval broker, rate limiter) at ~100%**.
  The exact threshold is a policy choice — pick one, put it in CI, and defend it.
- **[T2] Add an end-to-end "happy path" test a new user could trust.** We have
  `test_server.py` spinning up real subprocesses with mock backends (good), but
  nothing asserts the *value proposition* top to bottom: open a session → send
  turns that exceed the budget → eviction runs → the remembered fact is still
  recallable afterward. That single scenario, green in CI, is the closest thing
  to "it'll probably work" we can express without a real model. Make it explicit
  and named (e.g. `test_memory_survives_eviction_end_to_end`).
- **[T3] Close the real-model gap (or document it as out of scope for 1.0).**
  Everything runs against `StubChat`/mocks. The eviction + tagging logic is
  deterministic and well-tested at the harness level, which is why that's
  acceptable — but "talk to a real Qwen and watch memory behave" is untested.
  For 1.0 either (a) add one integration test against a small real model behind
  an opt-in marker (`@pytest.mark.integration`, run in CI nightly / on demand,
  not in the fast PR gate), or (b) explicitly document that real-model behavior
  is validated manually and the automated suite covers the deterministic core.
  Don't silently claim more than the suite proves.

**Also note for honesty's sake:** SSE "streaming" on `/turns` is currently a
*replay* of a finished completion (true streaming is M6, not done). If streaming
is a headline feature in the README, that needs to be labeled as replay until M6
lands — publishing a "streaming" claim that's actually buffered-under-the-hood is
a "doesn't feel like it'll work" moment for a careful user.

**Definition of done for Gate T:** CI reports a coverage number above the chosen
floor; there is a named end-to-end memory test in the fast gate; the real-model
gap is either covered by an opt-in integration test or explicitly documented; and
the README's feature claims (especially streaming) match what the code does.

---

## 4. The go/no-go checklist

### Publishable as a **0.x** (a real, installable, honest release)
- [ ] Naming decision made (§0); `pyproject` name + console script consistent.
- [ ] Version constant fixed and single-sourced (§1.1).
- [ ] `[build-system]`, classifiers, license holder, authors present (§1.2–1.3).
- [ ] `python -m build` produces a clean sdist+wheel; installing it into a fresh
      venv and running `purslane --help` (and the chosen CLI name) works.
- [ ] README's install + quickstart actually work from the installed package
      (not just editable).
- [ ] The `jail` limitation is stated plainly in the README (§2 S1) — even a 0.x
      must not let a dev be surprised by host-level exec.
- [ ] CI green: tests + lint + build.

### Ready for **1.0** (everything above, plus)
- [ ] **Gate S complete:** S1–S4 done (jail made unmissable, docker-downgrade
      surfaced, capability split or equivalent, fresh audit pass recorded).
- [ ] **Gate T complete:** coverage floor enforced in CI, named end-to-end memory
      test green, real-model gap covered-or-documented, feature claims accurate.
- [ ] M6 true streaming done **or** the README no longer implies live streaming.
- [ ] The 410 legacy-header shim decision (M5) resolved — a 1.0 shouldn't carry
      a known-to-be-deleted compat path without a stated removal date.
- [ ] A published `CHANGELOG` reflects the 1.0 contract and any breaking changes
      since 0.4.0.

**If you can't check every 1.0 box, ship the best 0.x you can (with the S1
warning) and don't call it 1.0.** A well-labeled 0.5 that's honest about its
limits beats a 1.0 with a quiet isolation hole.

---

## 5. Publish mechanics (once the boxes are checked)

- **Build:** `python -m build` → produces `dist/shatterling-<ver>.tar.gz` + `.whl`.
- **Upload (simplest):** `pip install twine`, then
  `twine upload dist/*` using a PyPI API token in `~/.pypirc`. Fine for a
  single-maintainer package.
- **Upload (better for a repo with CI): Trusted Publishing.** Configure an
  OIDC-based "Trusted Publisher" on the PyPI project pointing at this GitHub
  repo + workflow, then the release job uploads without storing a long-lived
  token in secrets. Requires the project to exist on PyPI first (create it once
  manually, or via a one-time token upload).
- **Release workflow sketch** (manual dispatch, gated on green tests):
  ```yaml
  # .github/workflows/publish.yml  (conceptual)
  on:
    workflow_dispatch:
      inputs: { version: { description: "e.g. 0.5.0" } }
  jobs:
    test:   { uses: ./.github/workflows/test_and_lint.yml }   # must pass
    publish:
      needs: test
      permissions: { id-token: write, contents: read }        # trusted publishing
      steps:
        - uses: actions/checkout@v4
        - run: |
            sed -i "s/^version = .*/version = \"${{ inputs.version }}\"/" pyproject.toml
            python -m build
            twine upload --non-interactive dist/*
  ```
  (Keep version bumping explicit and single-sourced — tie it to the same constant
  you fixed in §1.1 so `pyproject`, `__init__`, and `/version` always agree.)

---

## 6. Suggested sequencing

1. **Decide the name** (§0). Nothing else is worth doing until this is fixed.
2. **Cheap packaging fixes** (§1.1–1.4): version constant, build-system,
   classifiers, license holder. Half a day; unblocks a clean 0.x.
3. **S1 (jail warning) + T1 (coverage floor)** — the two highest-value items for
   your stated bar. Get a number and a loud warning.
4. **Ship the first 0.x** with honest docs. This is the "probably works, clearly
   labeled" release.
5. **Work toward 1.0:** S2–S3 (docker-downgrade surfacing, capability split),
   T2 (end-to-end memory test), M6 or de-scope streaming, fresh audit pass (S4),
   then the 1.0 checklist.

The through-line: **publish an honest 0.x now-ish, and treat 1.0 as "the day
Gate S and Gate T are both fully checked."**
