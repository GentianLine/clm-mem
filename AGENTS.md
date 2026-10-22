# AGENTS.md — How Agents Work On This Project

This file tells you (the agent) how to work on `clm-mem` inside the sandbox.
Follow it exactly; these conventions exist so parallel sessions never collide
and so every change lands reviewable and formatted.

## Repository & Remote

- Upstream: `https://github.com/glassresistor/clm-mem`
- The `origin` remote is set to the **SSH** URL:
  `git@github.com:glassresistor/clm-mem.git`
- **You cannot push.** The sandbox has no SSH keys / host trust. Do not try;
  it wastes a turn and looks like a failure. The human pushes whenever she wants
  to — your job is only to leave clean commits on a feature branch.
- To hand off: finish your branch, tell the user what branches exist and what
  each contains. She merges/pushes from her machine.

## One Session = One Workspace + One Branch

Every session gets its **own workspace directory** in the sandbox (a fresh clone
or worktree) and its **own branch off `main`**:

```bash
git clone https://github.com/glassresistor/clm-mem.git   # if not already cloned
cd clm-mem
git checkout main && git pull        # start from the tip of main
git checkout -b <type>/<short-topic> # e.g. fix/eviction-round2, feat/recall-stubs
```

Branch naming: `feat/…`, `fix/…`, `docs/…`, `refactor/…`. Never branch names like
`test`, `wip`, or my-session-1 — they collide with other agents.

Never commit directly to `main`. `main` only ever receives merged work.

## Environment: One venv Per Workspace

Each workspace gets its own virtualenv at `.venv/` (gitignored), installed in
**editable mode** so edits take effect immediately:

```bash
python3 -m venv .venv
.venv/bin/pip install -e "."        # package + pytest, editable
```

Notes:
- `pyproject.toml` is the single source of truth for dependencies.
  For a byte-for-byte reproducible env use `requirements.lock.txt` instead:
  `pip install -r requirements.lock.txt && pip install -e .`
- Always invoke tools through the venv: `.venv/bin/python`, `.venv/bin/pytest`,
  `.venv/bin/black`. Never assume a global `python` has the package installed.
- If `.venv/bin/black` is missing (some sandbox pip configs redirect scripts
  elsewhere), use the module form instead: `.venv/bin/python -m black ...`.
  Same for pytest: `.venv/bin/python -m pytest -q`.
- Verify the env works before you change anything:
  `.venv/bin/python -c "import clm_mem; print('ok')" && .venv/bin/pytest -q`

## Commits: Small, Frequent, Feature-Branch-Only

Commit **as you go** — after each coherent unit of work (a function, a test,
a doc section), not in one giant blob at the end:

```bash
git add <specific files>     # no `git add -A` blind commits
git commit -m "<scope>: <what changed>"
```

- Commits happen **only on your feature branch**. Never on `main`, never detached.
- Keep messages imperative and scoped (`eviction: keep pinned blocks in phase 1`).
- If a session is interrupted, that's fine — the work is already committed and
  recoverable on the branch.

## Formatting: black Is Always Safe

Run `black` on code **before shipping** (i.e. before finishing the task/handing
off). The workflow is commit → format → commit again, and you should feel no
fear doing it:

```bash
git add -A && git commit -m "<work>"          # 1. your logical change, committed
.venv/bin/black clm_mem tests                 # 2. let black reformat; it only ever
                                               #    makes safe, semantic-preserving changes
git add -A && git commit -m "style: black"    # 3. commit the formatting separately
```

Why this is safe and why the two commits matter:
- `black` never changes program semantics — it only normalizes formatting.
  So a formatter failure can't break your logic, and reviewers can separate
  *what you did* from *how it looks*.
- Committing before formatting means `git diff` of the style commit shows
  exactly what black touched.
- Do not configure/argue with black (no line-length tweaks, no reformatting
  loops). Accept its defaults. If CI complains elsewhere, that's not your problem.
- Run it on files you touched at minimum; on `clm_mem tests` when shipping a
  larger change. Markdown/pyproject are outside black's scope — it ignores them.

## Merging Into Main (Only When Asked)

Never merge on your own initiative. But when the user explicitly asks you to
**"merge changes into main"**, follow this protocol:

1. **Squash your feature branch into a single commit first.** The many small
   as-you-go commits are internal working history; what lands on `main` should
   be one commit with a clear description of the changes:

   ```bash
   git checkout <feature-branch>
   git reset --soft $(git merge-base main HEAD)   # collapse all commits into staged changes
   git commit -m "<one clear description of what this work accomplished>"
   ```

   (`git rebase -i $(git merge-base main HEAD)` with squash/fixup works too.)
   The message describes the *outcome* for a human reader — not each step you
   took along the way.
2. **Then merge** into `main` (fast-forward when possible):

   ```bash
   git checkout main && git merge <feature-branch>
   ```
3. Run `.venv/bin/python -m pytest -q` on `main` after merging; it must stay green.
4. Still do **not push** — the user pushes `main` to the remote when she wants.

## Tests & Shipping Checklist

Before calling work done:

1. `.venv/bin/pytest -q` passes (the suite is fast, ~30s; no network needed for
   the mock backends).
2. New behavior has a test in `tests/` (`test_harness.py`, `test_forgetting.py`,
   `test_server.py`, `test_web.py` cover the layers — put tests near the layer
   they exercise).
3. If you changed packaging/CLI entry points, reinstalled editable and re-ran
   `.venv/bin/clm-mem --help`.
4. Code blackened, formatting committed separately (see above).
5. Branch is clean (`git status` — nothing unstaged/untracked that matters).
6. Summarize to the user: branch name, what's in it, test status. She handles
   pushing/merging.

## Project Orientation (for a quick start)

- `clm_mem/harness.py` — the live window + eviction engine (phases 1–3).
- `clm_mem/tagging.py` — inline `<longterm>/<shortterm>` parsing and the
  post-turn router pass.
- `clm_mem/blocks.py`, `tokens.py`, `config.py` — block model, token counting,
  settings.
- `clm_mem/processes.py`, `remote.py`, `mcp.py`, `server.py` — serving surfaces.
- `clm_mem/static/` — the browser UI (mirrored to package data).
- README explains the CLM design and the three-phase eviction table; read it
  before touching eviction semantics.

## Non-Negotiables

1. New workspace + new branch off `main` per session.
2. venv per workspace, editable install, tools run from `.venv/bin/`.
3. Commit as you go, only on the feature branch, never on `main`.
4. black before shipping: commit, format, commit again — fearlessly.
5. Merge into `main` only when asked, and squash into one clear commit first.
   Otherwise don't push; the user does that.
6. Leave the tree clean and tests green so the next agent starts from a
   working base.
