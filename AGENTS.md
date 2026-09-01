# AGENTS.md

Guidance for AI coding agents. Details live in `docs/development.md` and `docs/architecture.md`; the facts below are the ones agents tend to get wrong.

## Safety rules (mandatory, user-mandated)

- **Privacy — nothing machine-specific in Git-tracked content** (public repo): no local absolute paths (`C:\Users\<USER>\...`, `H:\...`, home dirs), OS account names, hostnames, personal emails, or secrets (`.env`, keys, tokens, logs, transcripts). Use project-relative paths; replace home/user identifiers with `<HOME>` / `<USER>` / `<PROJECT_ROOT>` in examples and diagnostics.
- Before `git add` / `commit` / `push` / `gh pr create` / GitHub comments: inspect the staged diff and every outgoing title/body for path patterns like `C:\Users\...`, `/Users/...`, `/home/...` and anonymize. If safe sanitization is uncertain, stop and ask.
- **Irreversible actions**: before push to a shared branch, PR merge, release publish, or posting (email/Discord/SNS), show the exact final artifact and wait for explicit approval here. Plan approval ≠ artifact approval. Never pass a stopping point the user set.
- **Keep moving**: do not wait for the user to say "next" — once required approvals are given, proceed with the obvious next step without asking again; the user will stop you if it is wrong.
- **Text edits on Windows**: use Edit/Write tools, never `sed` / `printf` / heredocs on tracked files (CRLF risk). Japanese text destined for Windows-native files (`.ps1`, `.iss`) must be UTF-8 **with BOM** — PowerShell 5.1 silently reads BOM-less files as cp932. After bulk edits run `git diff --stat`; if unexpectedly large, check for line-ending corruption before committing.
- If another agent session may share this tree, work in a dedicated `git worktree`; never `git checkout` with uncommitted work. For multi-hour tasks, maintain `RESUME.md` (objective / done / exact next step / restore commands) at each milestone.
- **Measured claims only**: never state a limit, default, or perf characteristic as fact unless measured or read from config/source this session; label unverified statements as hypotheses.

## Layout

- `backend/` — FastAPI + SQLAlchemy (async, aiosqlite/SQLite). All Python commands run from `backend/`.
- `frontend/` — React 18 + Vite + TS. All Node commands run from `frontend/`.
- `config/` — `grading.yaml` (thresholds) and `carriers.yaml` (FLOLS geometry), read at runtime.
- Pipeline: ACMI ingest → detection → grading → SQLite → REST/WebSocket.
- Repo prose (README, docs/, plans/) is in Japanese — keep that style when editing it.

## Commands (mirror `.github/workflows/ci.yml`)

Backend (from `backend/`, after `pip install -e ".[dev]"`):
- `ruff check .` — line length 100; rule set is intentionally minimal (E4/E7/E9/F).
- `pytest -q`; single file: `pytest tests/test_grading.py -q`.
- CI also runs `alembic check` (drift): changing `app/models/entities.py` requires a new revision in `backend/migrations/` (`alembic revision -m "..."` from `backend/`) or CI fails.

Frontend (from `frontend/`):
- `npm ci` first (lockfile-driven).
- `npm run build` — runs `tsc -b`; this is the only typecheck (no separate script).
- `npm test` — vitest, single run (`npm test -- --watch` to watch).

## Running the backend locally

- Entry point is a factory: `uvicorn app.api.main:create_app --factory --port 8000` (from `backend/`).
- Config paths are **CWD-relative**: defaults `config/grading.yaml`, `config/carriers.yaml`, `./data/dlt.db` and `.env` only resolve from the repo root. Running from `backend/`, set e.g. `DLT_GRADING_CONFIG_PATH=../config/grading.yaml`.
- `DLT_ACMI_ENABLED=false` starts the API without the Tacview TCP client (otherwise it retries 127.0.0.1:31010 in the background forever).
- All settings are `DLT_`-prefixed env vars (`backend/app/config.py`); full list in `.env.example`.

## API conventions

- Routes are mounted at both `/api/v1` (current, Issue #38) and `/api` (deprecated alias). New code must use `/api/v1` — the README still shows plain `/api/...`. WS path: `/api/v1/ws/landings`.
- When `DLT_AUTH_TOKEN` is set: REST uses `X-Auth-Token` or `Authorization: Bearer`; WebSocket uses `?token=` (browsers can't send WS headers). `/api/health` stays public.
- Realtime notifications are two-phase: `landing` (provisional outcome) then `landing_update` (finalized). Keep both in sync when touching the pipeline or frontend socket handling.

## Gotchas

- Grading thresholds hot-reload from `config/grading.yaml` (mtime poll, ~5s). Regrade stored landings via `POST /api/landings/{id}/regrade` — raw approach samples are persisted in the DB for this.
- `config/carriers.yaml` FLOLS geometry and BURBLE thresholds are unverified estimates — don't treat grades as authoritative or silently "fix" the values.
- `DLT_DCSSB_REQUEST_SPACING_MS` (runway sweep via DCSServerBot) must not be lowered: `/airbase` runs Lua on the DCS simulation thread. Sweep results are cached per theatre in `cache/`.
- `*.acmi` recordings are gitignored (huge files); the only tracked one is the test fixture `backend/tests/fixtures/sample.acmi`. `Testdata/`, `*.db`, `data/` are local-only too.

## Tests

- pytest: `asyncio_mode = "auto"` — no decorators on async tests. Each test gets its own tmp-dir SQLite (`tests/conftest.py`); no external services needed.
- vitest: `environment: "node"` (not jsdom); tests colocated as `src/**/*.test.ts(x)`.

## Workflow

- Feature branch → PR into `main` (never push directly to `main` without approval per Safety rules).
- `.ai/` belongs to an external autodev tool (worktrees, `ai/autodev/*` branches) — not app code.
