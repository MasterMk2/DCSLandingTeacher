# AGENTS.md

Guidance for AI coding agents. Read `docs/development.md` or `docs/architecture.md` only when the task needs their development or architecture detail; the facts below are the ones agents tend to get wrong.

## Safety rules (mandatory, user-mandated)

- **Privacy**: do not include or externally expose any private, sensitive, secret, or environment-specific information; review and sanitize all outgoing changes and content before sharing, and stop to ask if safe handling is uncertain.
- **Irreversible actions**: before push to a shared branch, PR merge, release publish, or posting (email/Discord/SNS), show the exact final artifact and wait for explicit approval here. Plan approval is **NOT** artifact approval. Never pass a stopping point the user set.
- **Keep moving**: do not wait for the user to say "next" — once required approvals are given, proceed with the obvious next step without asking again; the user will stop you if it is wrong.
- When concurrent writers are confirmed on the same worktree, use a dedicated `git worktree`; never `git checkout` with uncommitted work. Keep `RESUME.md` (objective / done / exact next step / restore commands) only for an expected handoff, interruption, or user-requested long-running task.
- **Measured claims only**: never state a limit, default, or perf characteristic as fact unless measured or read from config/source this session; label unverified statements as hypotheses.

## Layout

- `backend/` — FastAPI + SQLAlchemy (async, psycopg/PostgreSQL). Application Python commands run from `backend/`.
- `migration-job/` — Alembic migration validation; its Python commands run from `migration-job/`.
- `frontend/` — React 18 + Vite + TS. All Node commands run from `frontend/`.
- `config/` — `grading.yaml` (thresholds) and `carriers.yaml` (FLOLS geometry), read at runtime.
- Pipeline: ACMI ingest → detection → grading → PostgreSQL → REST/WebSocket.
- Repo prose (README, docs/, plans/) is in Japanese — keep that style when editing it.

## Commands

Run the applicable checks according to the specification and project configuration, and keep `.github/workflows/ci.yml` aligned accordingly.

Backend (from `backend/`, after `uv sync --frozen --no-install-project`):

- `uv run ruff check .`
- `uv run basedpyright path/to/edited_file.py`: run only on edited Python files; do not require a full-backend pass.
- `uv run pytest -q`

Migration job (from `migration-job/`, when changing `migration-job/`):

- `uv sync --frozen --no-install-project`
- `uv run ruff check .`
- `uv run basedpyright`
- `uv run pytest -q`

Frontend (from frontend/):

- `npm ci`
- `npm run build`
- `npm test`

## Running the backend locally

- Entry point is a factory. To run the API without Tacview from PowerShell in `backend/`: `$env:PYTHONPATH = "src"; $env:DLT_ACMI_ENABLED = "false"; $env:DLT_GRADING_CONFIG_PATH = "../config/grading.yaml"; $env:DLT_CARRIERS_CONFIG_PATH = "../config/carriers.yaml"; uv run uvicorn app.api.main:create_app --factory --port 8000`.
- Config paths and `.env` are **CWD-relative**. Running from `backend/`, use `DLT_GRADING_CONFIG_PATH=../config/grading.yaml` and `DLT_CARRIERS_CONFIG_PATH=../config/carriers.yaml`; create `backend/.env` when using the example environment file. The default `DLT_DATABASE_URL` targets local PostgreSQL.
- `DLT_ACMI_ENABLED=false` starts the API without the Tacview TCP client (otherwise it retries 127.0.0.1:31010 in the background forever).
- All settings are `DLT_`-prefixed env vars (`backend/src/app/config.py`); full list in `.env.example`.

## API conventions

- Routes are mounted at both `/api/v1` and `/api` (deprecated alias). New code must use `/api/v1`; WebSocket path: `/api/v1/ws/landings`.
- When `DLT_AUTH_TOKEN` is set: REST uses `X-Auth-Token` or `Authorization: Bearer`; WebSocket uses `?token=` (browsers can't send WS headers). `/api/v1/health` and its `/api/health` alias stay public.
- Realtime notifications are two-phase: `landing` (provisional outcome) then `landing_update` (finalized). Keep both in sync when touching the pipeline or frontend socket handling.

## Constraints

- Do not silently change grading or carrier-geometry values without checking the relevant specification.
- Do not lower `DLT_DCSSB_REQUEST_SPACING_MS` unless the specification explicitly requires it.
- Do not add local ACMI recordings, databases, or import data to version control.

## Workflow

- Feature branch → PR into `main` (never push directly to `main` without approval per Safety rules).
- `.ai/` belongs to an external autodev tool (worktrees, `ai/autodev/*` branches) — not app code.
