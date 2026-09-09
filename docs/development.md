# 開発ガイド

開発環境の構築とテスト実行手順。アーキテクチャの全体像は [`architecture.md`](architecture.md) を参照してください。

## 必要要件

| ツール | バージョン |
|---|---|
| Python | 3.11 以上 |
| Node.js | 20 以上（LTS 推奨） |
| PostgreSQL | API をネイティブ起動する場合に必要 |
| Docker（任意） | Docker Desktop または Engine + Compose v2。Compose で PostgreSQL と migration job を起動できる |

## バックエンド

### 環境構築

```bash
cd backend
uv sync --frozen --no-install-project
```

`backend/` に `.env` を作成し、`DLT_DATABASE_URL` をローカル PostgreSQL に合わせて設定します。

```bash
cp ../.env.example .env
```

API 単体で動かす場合は ACMI 受信を無効化すると Tacview なしで起動できます:

```bash
# Windows (cmd)
set PYTHONPATH=src&& set DLT_ACMI_ENABLED=false&& set DLT_GRADING_CONFIG_PATH=../config/grading.yaml&& set DLT_CARRIERS_CONFIG_PATH=../config/carriers.yaml&& uv run uvicorn app.api.main:create_app --factory --port 8000
# Linux
PYTHONPATH=src DLT_ACMI_ENABLED=false DLT_GRADING_CONFIG_PATH=../config/grading.yaml DLT_CARRIERS_CONFIG_PATH=../config/carriers.yaml uv run uvicorn app.api.main:create_app --factory --port 8000
```

- 動作確認: `http://localhost:8000/api/v1/health`（Compose のヘルスチェックは互換 endpoint の `/api/health` を使用）
- OpenAPI: `http://localhost:8000/docs`

### フロントエンド

リポジトリルートで実行します。

```bash
cd frontend
npm ci
npm run dev   # http://localhost:5173 （/api を :8000 へプロキシ）
```

プロキシ先を変更する場合:

```bash
DLT_BACKEND_URL=http://localhost:9000 npm run dev   # Linux/macOS
# PowerShell: $env:DLT_BACKEND_URL="http://localhost:9000"; npm run dev
```

本番相当の確認（Docker Compose）:

```bash
docker compose up --build
```

## データベースマイグレーション（Alembic）

スキーマ変更は `migration-job/` の Alembic プロジェクトで管理します。API はスキーマを作成・更新せず、Compose の `migration-job` が PostgreSQL の正常起動後に適用します。

リポジトリルートで `.env` に `DLT_POSTGRES_USER`、`DLT_POSTGRES_PASSWORD`、`DLT_POSTGRES_DB` を設定し、未適用マイグレーションを適用します。

```bash
docker compose run --rm migration-job
```

- スクリプト配置: [`migration-job/migrations/`](../migration-job/migrations/)
- 既存の SQLite ボリュームは PostgreSQL へ自動移行されません。データを残す場合は
  別途エクスポート・インポートしてから切り替えます。

## テスト

### バックエンド（pytest）

```bash
cd backend
uv run pytest -q                 # 全テスト
uv run pytest tests/unit/app/grading/test_land_grader.py -q   # 特定ファイル
```

- `asyncio_mode = "auto"` のため async テストはデコレータ不要
- フィクスチャ: `tests/fixtures/sample.acmi`、共通ヘルパーは `tests/conftest.py` / `tests/helpers.py`
- バックエンドの単体テストは一時 SQLite を使用する。実運用の接続先とスキーマ管理は PostgreSQL と `migration-job`。

### フロントエンド（vitest）

```bash
cd frontend
npm test          # vitest run（1 回実行）
npm test -- --watch   # ウォッチモード
```

### Lint

```bash
cd backend
uv run ruff check .
uv run ruff check --fix .   # 自動修正可能な違反を修正
```

ルールセットは [`backend/pyproject.toml`](../backend/pyproject.toml) の `[tool.ruff.lint]`（最小構成: E4/E7/E9/F）で管理しています。

## Docker での検証

```bash
docker compose up --build     # ビルド + 起動
curl http://localhost:8000/api/health  # .env.example の DLT_PORT=8000 を使う場合
docker compose down           # ボリューム postgres_data は保持される
docker compose down -v        # データも削除
```

フロントエンド単体イメージ（nginx 構成、任意）:

```bash
docker build -f docker/frontend.Dockerfile -t dlt-frontend .
```

## CI

`.github/workflows/ci.yml` が push / PR ごとに実行されます:

| ジョブ | 内容 |
|---|---|
| backend | Python 3.11 / `uv sync --frozen --no-install-project` → `uv run ruff check .` → `uv run pytest -q` |
| frontend | Node 20 / `npm ci` → `npm run build` → `npm test` |
| migration-job | Python 3.11 / `uv sync --frozen --no-install-project` → `uv run ruff check .` → `uv run basedpyright` → `uv run pytest -q` |
| compose | PostgreSQL 環境変数を設定して `docker compose config --quiet` → `docker compose build` |

ローカルで CI と同じことを確認するには上記コマンドをそのまま実行してください。シークレットは不要です。

## コミット方針

- ブランチ: `main`（安定）＋ feature branch → Pull Request
- コミットメッセージ・PR 本文にローカルパスや個人名を含めないこと（公開リポジトリのため）
