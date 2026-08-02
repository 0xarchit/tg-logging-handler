# GitHub Setup — `tg-logging-handler`

End-to-end guide for repo structure, branch strategy, GitHub Environments, CI/CD pipelines, and PyPI publishing. Written so a first-time package author (or a coding agent) can follow it step by step with no gaps.

---

## 1. Repo Structure

```
tg-logging-handler/
├── .github/
│   ├── workflows/
│   │   ├── ci.yml              # lint + typecheck + test, every push/PR
│   │   ├── publish-testpypi.yml # publish to TestPyPI on every push to main
│   │   └── publish-pypi.yml     # publish to real PyPI on version tag
│   ├── ISSUE_TEMPLATE/
│   │   ├── bug_report.md
│   │   └── feature_request.md
│   ├── PULL_REQUEST_TEMPLATE.md
│   └── dependabot.yml
├── tg_logging_handler/                # see ARCHITECTURE.md
├── tests/
├── docs/                          # PRD.md, ARCHITECTURE.md, API_SPEC.md, CODING_STANDARDS.md, TESTING.md, ROADMAP.md
├── .pre-commit-config.yaml
├── pyproject.toml
├── README.md
├── CHANGELOG.md
├── LICENSE
└── .gitignore
```

---

## 2. Environments — What They Mean For a Library

A library has no deployed "prod server," but the same three-stage discipline still applies, mapped onto **publish targets** instead of servers:

| Stage | Equivalent here | Trigger |
|---|---|---|
| **Testing** | CI test matrix (ruff/mypy/pytest) | every push + every PR, all branches |
| **Preview/Staging** | **TestPyPI** — a real but separate index for dry-run installs | every push to `main` |
| **Production** | **PyPI** (the real, public index) | pushing a `v*.*.*` git tag, gated by a protected GitHub Environment |

This gives you a genuine dry run — install from TestPyPI into a clean venv and verify the package actually works — before anything goes to the real index where you can't easily un-publish.

---

## 3. Branch Strategy

Keep it simple for a solo/small project:

- `main` — always green (CI passing), always installable from TestPyPI.
- Feature branches: `feat/batching`, `fix/retry-backoff`, etc. — PR into `main`.
- Tags: `v0.1.0`, `v0.1.1`, `v1.0.0` — these are what trigger real PyPI publishes. Tag only from `main`, only after CI is green on the commit being tagged.

**Branch protection on `main`** (Settings → Branches → Add rule):
- Require a pull request before merging (even solo — keeps history clean and forces CI to run).
- Require status checks to pass before merging → select the `ci.yml` job(s).
- Require branches to be up to date before merging.
- Do not allow force-pushes to `main`.

---

## 4. GitHub Environments Setup

Environments give you approval gates and environment-scoped secrets — this is what makes the "production" publish deliberate instead of accidental.

Go to **Settings → Environments** and create two:

### `testpypi`
- No protection rules needed (low stakes, easy to re-publish under a new dev version).
- Add environment secret: none needed if using Trusted Publishing (recommended, see §6) — otherwise `TEST_PYPI_API_TOKEN`.

### `pypi`
- **Required reviewers**: add yourself. This means every real publish requires a manual "Approve" click in the Actions UI — your safety net against a mistaken tag push publishing garbage to the real index.
- **Deployment branches**: restrict to tags matching `v*` only (Settings → Environments → `pypi` → Deployment branches and tags → "Selected branches and tags" → add tag rule `v*`).
- Add environment secret: none needed if using Trusted Publishing — otherwise `PYPI_API_TOKEN`.

---

## 5. CI Workflow — `.github/workflows/ci.yml`

Runs on every push and PR. This is the "testing environment."

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        python-version: ["3.9", "3.10", "3.11", "3.12", "3.13"]
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python ${{ matrix.python-version }}
        uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
          cache: "pip"

      - name: Install package + dev deps
        run: pip install -e ".[dev]"

      - name: Lint (ruff check)
        run: ruff check .

      - name: Format check (ruff format)
        run: ruff format --check .

      - name: Type check (mypy --strict)
        run: mypy --strict tg_logging_handler

      - name: Test with coverage
        run: pytest --cov=tg_logging_handler --cov-report=term-missing --cov-report=xml --cov-fail-under=90

      - name: Upload coverage
        uses: codecov/codecov-action@v4
        with:
          files: coverage.xml
        continue-on-error: true   # don't fail CI just because Codecov upload had a hiccup

  build-check:
    runs-on: ubuntu-latest
    needs: test
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Build sdist + wheel
        run: |
          pip install build
          python -m build
      - name: Check package metadata
        run: |
          pip install twine
          twine check dist/*
      - name: Verify py.typed is included
        run: |
          python -m zipfile -l dist/*.whl | grep py.typed
```

`build-check` catches packaging mistakes (missing `py.typed`, broken `pyproject.toml` metadata) on every PR — long before a tag/publish attempt.

---

## 6. PyPI Publishing — Trusted Publishing (recommended, no stored secrets)

Use **PyPI Trusted Publishing** (OIDC) instead of long-lived API tokens. No secret ever lives in GitHub; PyPI trusts GitHub Actions directly per-repo/per-workflow.

### One-time setup on PyPI/TestPyPI
1. Create an account and, separately, register the project name on both:
   - https://test.pypi.org — for the `testpypi` environment.
   - https://pypi.org — for the `pypi` environment (do this once you're confident in the final package name — see PRD.md open question on naming).
2. On each site: **Account → Publishing → Add a new pending publisher**:
   - PyPI Project Name: `tg-logging-handler` (or final confirmed name)
   - Owner: your GitHub username/org
   - Repository name: `tg-logging-handler`
   - Workflow name: `publish-pypi.yml` (or `publish-testpypi.yml` on the TestPyPI side)
   - Environment name: `pypi` (or `testpypi`)
3. No API token needed — the workflow below authenticates via OIDC automatically.

### `.github/workflows/publish-testpypi.yml`
```yaml
name: Publish to TestPyPI

on:
  push:
    branches: [main]

permissions:
  id-token: write   # required for OIDC trusted publishing

jobs:
  publish:
    runs-on: ubuntu-latest
    environment: testpypi
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install build
      - run: python -m build
      - name: Publish to TestPyPI
        uses: pypa/gh-action-pypi-publish@release/v1
        with:
          repository-url: https://test.pypi.org/legacy/
          skip-existing: true   # main can be pushed multiple times without a version bump; don't fail the run
```

### `.github/workflows/publish-pypi.yml`
```yaml
name: Publish to PyPI

on:
  push:
    tags:
      - "v*.*.*"

permissions:
  id-token: write

jobs:
  publish:
    runs-on: ubuntu-latest
    environment: pypi   # this is what triggers the required-reviewer approval gate
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install build twine
      - run: python -m build
      - run: twine check dist/*
      - name: Publish to PyPI
        uses: pypa/gh-action-pypi-publish@release/v1
```

Because this workflow targets the `pypi` environment (with required reviewers, tag-restricted deployment branches), pushing a `v*` tag does **not** immediately publish — it opens a pending deployment that you must manually approve in the Actions tab. This is your last checkpoint before something becomes public and effectively permanent.

---

## 7. Version Bump + Release Process

Manual checklist (v1 has no version automation — keep it simple until there's a reason not to):

1. On `main`, confirm CI is green and TestPyPI publish succeeded on the latest commit.
2. `pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ tg-logging-handler` in a fresh venv, run the README quickstart manually against a throwaway bot (TESTING.md §5 pre-release checklist).
3. Bump `version` in `pyproject.toml`.
4. Add a `CHANGELOG.md` entry under a new version heading (Keep a Changelog format).
5. Commit: `chore: release v0.1.0`.
6. Tag: `git tag v0.1.0 && git push origin v0.1.0`.
7. Go to the Actions tab → approve the pending `pypi` environment deployment.
8. Verify on https://pypi.org/project/tg-logging-handler/ that the new version is live.
9. Create a GitHub Release from the tag (Releases → Draft a new release → select tag → paste CHANGELOG entry as notes).

---

## 8. Supporting Files

### `.gitignore` (Python-specific essentials)
```
__pycache__/
*.py[cod]
*.egg-info/
dist/
build/
.venv/
.mypy_cache/
.ruff_cache/
.pytest_cache/
.coverage
coverage.xml
```

### `.github/dependabot.yml`
```yaml
version: 2
updates:
  - package-ecosystem: "pip"
    directory: "/"
    schedule:
      interval: "weekly"
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
```

### `.pre-commit-config.yaml`
```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.6.9
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v1.11.2
    hooks:
      - id: mypy
        additional_dependencies: [httpx]
        args: [--strict]
        files: ^tg_logging_handler/
```
Install once locally: `pre-commit install`.

### `.github/PULL_REQUEST_TEMPLATE.md`
```markdown
## What
<!-- What does this change? -->

## Why
<!-- Link to PRD.md FR-id / ROADMAP.md milestone if applicable -->

## Checklist
- [ ] `ruff check .` / `ruff format --check .` pass locally
- [ ] `mypy --strict tg_logging_handler` passes locally
- [ ] `pytest --cov=tg_logging_handler --cov-fail-under=90` passes locally
- [ ] CHANGELOG.md updated (if user-facing change)
```

---

## 9. Repo-Level Settings Checklist (one-time, via GitHub UI)

- [ ] Settings → General → Features: enable Issues, disable Wiki/Projects unless you want them.
- [ ] Settings → General → Pull Requests: enable "Automatically delete head branches" (keeps branch list clean).
- [ ] Settings → Branches: protection rule on `main` per §3.
- [ ] Settings → Environments: `testpypi` and `pypi` per §4.
- [ ] Settings → Secrets and variables: **none required** if using Trusted Publishing (§6) — this is the point, don't add API tokens unless you deliberately choose the token-based fallback instead.
- [ ] Settings → Tags: none required to protect explicitly since the `pypi` environment already restricts deployment to `v*` tags, but you may optionally add a tag protection rule (Settings → Tags → New rule → `v*`) restricting who can push release tags, useful once collaborators join.
- [ ] Add a `LICENSE` file (MIT recommended for a small utility library — permissive, expected by users of this kind of package).
- [ ] Repo description + topics (`python`, `logging`, `telegram`, `telegram-bot`) filled in — this is what makes it discoverable and is a small but real part of "does this read as a maintained, real package" for anyone (including future employers) checking it out.

---

## 10. Order of Operations (do this once, in sequence)

1. Create the GitHub repo, push initial scaffold (empty `tg_logging_handler/` package + `pyproject.toml` + this `docs/` folder).
2. Set up branch protection (§3) and both Environments (§4) before writing any real code — this way CI is enforced from commit #1, not bolted on later.
3. Add `ci.yml` (§5), confirm it goes green on a trivial first PR.
4. Implement M0 (per ROADMAP.md) behind PRs into `main`.
5. Register the project name on TestPyPI, add `publish-testpypi.yml`, confirm the pending-publisher OIDC link works and a dev build lands on TestPyPI.
6. Continue M1–M3 with TestPyPI publishing on every merge to `main` as your continuous "does this actually install and import" signal.
7. Once M3 exit criteria are met (ROADMAP.md), register the name on real PyPI, add `publish-pypi.yml`, tag `v0.1.0`, approve the deployment, done.
