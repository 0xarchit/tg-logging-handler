# GitHub Setup — `tg-logging-handler`

End-to-end guide for repo structure, branch strategy, GitHub Environments, CI/CD
pipelines, code scanning, and PyPI publishing. Written so a first-time package
author (or a coding agent) can follow it step by step with no gaps.

This document matches the workflow files actually committed under `.github/`.
Toolchain: **uv** (lockfile-driven), **ruff** + **mypy --strict** + **pytest**,
**PyPI Trusted Publishing** (OIDC — no stored tokens), and **CodeQL** advanced
code scanning.

---

## 1. Repo Structure

```
tg-logging-handler/
├── .github/
│   ├── workflows/
│   │   ├── ci.yml                # lint + typecheck + test matrix + build, every push/PR
│   │   ├── codeql.yml            # CodeQL security scan (push/PR + weekly)
│   │   ├── publish-testpypi.yml  # publish to TestPyPI on every push to main
│   │   └── publish-pypi.yml      # publish to real PyPI on a published GitHub Release
│   ├── PULL_REQUEST_TEMPLATE.md
│   └── dependabot.yml            # pip + github-actions, weekly
├── tg_logging_handler/           # see docs/ARCHITECTURE.md
├── tests/
├── docs/                         # PRD, ARCHITECTURE, API_SPEC, CODING_STANDARDS, TESTING, ROADMAP
├── .pre-commit-config.yaml
├── .python-version               # 3.13 (local/dev interpreter for uv)
├── uv.lock                       # pinned dev+runtime deps — CI installs from this
├── pyproject.toml
├── README.md
├── CHANGELOG.md
├── LICENSE
└── .gitignore
```

---

## 2. CI/CD At a Glance

A library has no deployed "prod server," but the three-stage discipline still
applies, mapped onto **publish targets** instead of servers:

| Stage | What runs | Trigger |
|---|---|---|
| **Test** | `ci.yml` — ruff / mypy / pytest matrix (py3.9–3.14) + build check | every push + every PR |
| **Scan** | `codeql.yml` — CodeQL security-extended + security-and-quality | push/PR to `main` + weekly |
| **Preview** | `publish-testpypi.yml` — publish to **TestPyPI** + install-back smoke test | **CI green** on `main` |
| **Production** | `publish-pypi.yml` — publish to **PyPI** + install-back smoke test | a **GitHub Release** is published |

**The publish stages are gated on tests, not run beside them.** TestPyPI publish
fires from `workflow_run` — it only starts *after* `ci.yml` reports `success` on
`main`, so a red matrix (or a missing `CODECOV_TOKEN`) blocks the publish instead
of racing it. PyPI publish fires on a Release, which you cut only from a green
`main` (branch protection in §3 enforces CI before the release commit exists).

Both publish workflows end with a **`smoke-test` job**: install the just-published
package *from the index* into clean py3.9 and py3.13 venvs and assert the public
API imports (`TelegramLoggingHandler`, `TGLoggingHandler`, `TelegramConfigError`,
`__version__`). This is the cross-check that the built artifact is actually
installable and importable from the real index — not just that it built locally.
Install uses a 5× retry with backoff to absorb index propagation lag.

So the full chain the pieces cross-test each other along is:
**local gate (`uv run …`) → CI matrix (py3.9–3.14) → TestPyPI publish + install-back → Release → PyPI publish + install-back.**

---

## 3. Branch Strategy

Keep it simple for a solo/small project:

- `main` — always green (CI passing), always installable from TestPyPI.
- Feature branches: `feat/batching`, `fix/retry-backoff`, etc. — PR into `main`.
- Releases: cut a **GitHub Release** (with a `v0.1.0` tag) — this is what triggers
  the real PyPI publish. Release only from `main`, only after CI is green on the
  commit being released.

**Branch protection on `main`** (Settings → Branches → Add rule):
- Require a pull request before merging (even solo — keeps history clean and forces CI to run).
- Require status checks to pass before merging → select the `ci.yml` matrix jobs and the CodeQL job.
- Require branches to be up to date before merging.
- Do not allow force-pushes to `main`.

---

## 4. GitHub Environments Setup

Environments give you approval gates and environment-scoped protection — this is
what makes the "production" publish deliberate instead of accidental. With
Trusted Publishing there are **no secrets to store**; the environment name is
part of what PyPI trusts, so it must match the workflow exactly.

Go to **Settings → Environments** and create two:

### `testpypi`
- No protection rules needed (low stakes, easy to re-publish under a new dev version).
- No secrets — OIDC handles auth (see §6).

### `pypi`
- **Required reviewers**: add yourself. Every real publish then needs a manual
  "Approve" click in the Actions UI — your safety net against a mistaken release
  publishing garbage to the real index.
- **Deployment branches and tags**: restrict to tags matching `v*` only, so only
  a real version tag can deploy this environment.
- No secrets — OIDC handles auth (see §6).

---

## 5. CI Workflow — `.github/workflows/ci.yml`

Runs on every push and PR. Installs from `uv.lock` (`uv sync --frozen`) so CI
uses the exact pinned dev toolchain, then runs the same gate you run locally.

- **Matrix**: Python `3.9`–`3.14`, `fail-fast: false` so one version's failure
  doesn't hide the others. `uv sync --frozen --python <ver>` pins each leg.
- **Gate** (mirrors local): `ruff check .` → `ruff format --check .` →
  `mypy` (config-driven, `--strict`, targets `tg_logging_handler` + `tests`) →
  `pytest --cov` (the `fail_under = 90` gate in `pyproject.toml` is authoritative).
- **Coverage upload**: Codecov, once (on the 3.13 leg — matches `.python-version`),
  with `fail_ci_if_error: true` so a broken upload surfaces instead of silently
  passing. Requires the `CODECOV_TOKEN` repo secret (§10); the badge is in the
  README.
- **`build` job** (after `test`): `uv build`, `uvx twine check dist/*`, and a
  grep that fails if `tg_logging_handler/py.typed` is missing from the wheel.
  Catches packaging mistakes (broken metadata, dropped `py.typed`) on every PR,
  long before a publish attempt.

> Why `uv run mypy` with no args: `pyproject.toml` already sets
> `files = ["tg_logging_handler", "tests"]` and `strict = true`, so passing paths
> on the CLI would only risk drifting from the committed config.

---

## 6. Security Scanning — `.github/workflows/codeql.yml`

**Advanced CodeQL setup** (a committed workflow, not GitHub's "default" toggle),
so the query suites and triggers live in-repo and are reviewable:

- **Language**: `python`, `build-mode: none` (pure Python — nothing to compile).
- **Queries**: `security-extended,security-and-quality` — the broadest first-party
  suites, more than the default `security` set.
- **Triggers**: push + PR to `main`, plus a weekly `schedule` so newly published
  CodeQL rules catch latent issues even when the code is quiet.
- **Permissions**: `security-events: write` is required to upload results to the
  repo's **Security → Code scanning** tab.

One-time: **Settings → Code security → Code scanning** — if "Default" was ever
enabled, switch it off so it doesn't run alongside this advanced workflow.

---

## 7. PyPI Publishing — Trusted Publishing (OIDC, no stored secrets)

Both publish workflows authenticate with **PyPI Trusted Publishing** (OIDC) via
`permissions: id-token: write` — no API token ever lives in GitHub.

### One-time setup on PyPI / TestPyPI
Register a **pending publisher** on each index that must match the workflow
exactly. On each site: **Account → Publishing → Add a new pending publisher**:

| Field | TestPyPI (test.pypi.org) | PyPI (pypi.org) |
|---|---|---|
| PyPI Project Name | `tg-logging-handler` | `tg-logging-handler` |
| Owner | `0xarchit` | `0xarchit` |
| Repository name | `tg-logging-handler` | `tg-logging-handler` |
| Workflow name | `publish-testpypi.yml` | `publish-pypi.yml` |
| Environment name | `testpypi` | `pypi` |

Do the TestPyPI one first (low stakes) and confirm a dev build lands, then do
PyPI once the name is final. No token needed — the workflows authenticate via
OIDC automatically.

### `publish-testpypi.yml` (gated on CI success on `main`)
Triggered by `workflow_run` on the **CI** workflow — the `publish` job runs only
when `github.event.workflow_run.conclusion == 'success'`, so nothing publishes
off a red build. It checks out the exact commit CI validated
(`workflow_run.head_sha`), builds with `uv build`, and publishes to TestPyPI via
`pypa/gh-action-pypi-publish@release/v1` with `skip-existing: true` — `main` can
be pushed without a version bump, so re-uploading the same version must not fail
the run. A dependent **`smoke-test`** job then installs the package back from
TestPyPI (py3.9 + py3.13, 5× retry for propagation lag) and asserts the public
API imports.

> `workflow_run` triggers only fire for workflow files on the repo's **default
> branch** — this works once `main` has these files, which it will after the
> first push. On a brand-new repo the very first CI run won't have a publish
> workflow to trigger yet; it kicks in from the second push onward.

### `publish-pypi.yml` (on published GitHub Release)
Runs against the `pypi` environment (required-reviewer gate), builds, runs
`twine check`, then publishes. A Release is cut only from a green `main` (branch
protection in §3 requires CI to pass before the release commit exists), so CI
gates this path too. Because it targets a protected environment, publishing a
Release does **not** upload immediately — it opens a pending deployment you must
approve in the Actions tab. After the upload, a dependent **`smoke-test`** job
installs the package back from **real PyPI** (py3.9 + py3.13) and asserts the
public API imports. That approval is the last checkpoint
before something becomes public and effectively permanent.

---

## 8. Release Process

`version` is set manually in `pyproject.toml` (no version automation in v1 —
keep it simple until there's a reason not to). To cut a release:

1. On `main`, confirm CI + CodeQL are green and the TestPyPI publish succeeded on
   the latest commit.
2. In a fresh venv, dry-run the install from TestPyPI and run the README
   quickstart against a throwaway bot (see `docs/TESTING.md`):
   ```
   pip install --index-url https://test.pypi.org/simple/ \
       --extra-index-url https://pypi.org/simple/ tg-logging-handler
   ```
3. Bump `version` in `pyproject.toml` (drop the `.dev0` suffix for a real release,
   e.g. `0.1.0`).
4. Add a `CHANGELOG.md` entry under a new version heading (Keep a Changelog format).
5. Commit + push (PR into `main`): `chore: release v0.1.0`.
6. **Draft a new GitHub Release** (Releases → Draft a new release), create the tag
   `v0.1.0` on the release commit, paste the CHANGELOG entry as notes, and
   **Publish** it. Publishing the Release triggers `publish-pypi.yml`.
7. Actions tab → **approve** the pending `pypi` environment deployment.
8. Verify at https://pypi.org/project/tg-logging-handler/ that the version is live.

---

## 9. Supporting Files (as committed)

### `.pre-commit-config.yaml`
ruff (`--fix`), ruff-format, and mypy (`httpx` + `pytest` as typed deps, scoped to
`tg_logging_handler/` and `tests/`). Install once locally:
```
uv run pre-commit install
```

### `.github/dependabot.yml`
Weekly updates for two ecosystems, each grouped into a single PR:
- `pip` — runtime + dev deps from `pyproject.toml`.
- `github-actions` — the action versions pinned in these workflows.

### `.github/PULL_REQUEST_TEMPLATE.md`
What / Why / Checklist, where the checklist is the local gate:
`uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`,
`uv run pytest --cov=tg_logging_handler`, and a CHANGELOG reminder.

### `pyproject.toml` → `[project.urls]`
Homepage / Repository / Issues / Changelog all point at
`github.com/0xarchit/tg-logging-handler` — these render as links on the PyPI page.

---

## 10. Repo-Level Settings Checklist (one-time, via GitHub UI)

- [ ] Settings → General → Pull Requests: enable "Automatically delete head branches".
- [ ] Settings → Branches: protection rule on `main` per §3 (include CI + CodeQL checks).
- [ ] Settings → Environments: `testpypi` and `pypi` per §4.
- [ ] Settings → Code security → Code scanning: ensure **Default** CodeQL is **off**
      (the advanced `codeql.yml` replaces it); Dependabot alerts on.
- [ ] Settings → Secrets and variables: **none required** — Trusted Publishing (§7)
      is the whole point; don't add API tokens unless you deliberately switch to the
      token-based fallback.
- [ ] Register pending publishers on TestPyPI and PyPI per §7.
- [ ] Repo description + topics (`python`, `logging`, `telegram`, `telegram-bot`) filled in.

---

## 11. Order of Operations (first-time bring-up)

1. Push the scaffold (package + `pyproject.toml` + `uv.lock` + this `docs/` folder + `.github/`).
2. Set up branch protection (§3) and both Environments (§4) before real code lands,
   so CI is enforced from commit #1.
3. Confirm `ci.yml` and `codeql.yml` go green on a trivial first PR.
4. Register the TestPyPI pending publisher (§7), push to `main`, confirm a dev build
   lands on TestPyPI.
5. Develop behind PRs into `main`, using TestPyPI publishing on every merge as your
   continuous "does this actually install and import" signal.
6. When ready to ship: register the PyPI pending publisher, bump the version, publish a
   GitHub Release, approve the `pypi` deployment (§8), done.


