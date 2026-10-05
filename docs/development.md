# 開発ガイド

開発環境の構築とテスト実行手順。アーキテクチャの全体像は [`architecture.md`](architecture.md) を参照してください。
固定版・復元順序・doctor/sync/check・適用範囲と例外は [開発基盤の共通契約 0.1](development-foundation.md) に従います。

## 必要要件

| ツール | バージョン |
|---|---|
| Python / uv | 3.11.15 / 0.12.19（migration-job は 3.11 系を維持） |
| Node.js / npm | 22.23.3 / 11.9.0 |
| PostgreSQL | API をネイティブ起動する場合に必要 |
| Docker（任意） | Docker Desktop または Engine + Compose v2。Compose で PostgreSQL と migration job を起動できる |

## バックエンド

### 環境構築

```bash
cd backend
uv python install --no-bin --no-registry
uv lock --check
uv sync --frozen --no-install-project
uv run --no-sync python ../tools/dev.py doctor
```

`backend/` に `.env` を作成し、`DLT_DATABASE_URL` をローカル PostgreSQL に合わせて設定します。

```bash
cp ../.env.example .env
```

API はスキーマを作成しません。起動前に、設定した `DLT_DATABASE_URL` と同じ
PostgreSQL の接続先へ [migration-job を適用](#データベースマイグレーションalembic) してください。
新規 DB は migration-job の実行後に API を起動します。

API 単体で動かす場合は ACMI 受信を無効化すると Tacview なしで起動できます:

```bash
# PowerShell
$env:PYTHONPATH="src"; $env:DLT_ACMI_ENABLED="false"; $env:DLT_GRADING_CONFIG_PATH="../config/grading.yaml"; $env:DLT_CARRIERS_CONFIG_PATH="../config/carriers.yaml"; uv run --no-sync uvicorn app.api.main:create_app --factory --port 8000
# Linux
PYTHONPATH=src DLT_ACMI_ENABLED=false DLT_GRADING_CONFIG_PATH=../config/grading.yaml DLT_CARRIERS_CONFIG_PATH=../config/carriers.yaml uv run --no-sync uvicorn app.api.main:create_app --factory --port 8000
```

- 動作確認: `http://localhost:8000/api/v1/health`（Compose のヘルスチェックは互換 endpoint の `/api/health` を使用）
- OpenAPI: `http://localhost:8000/docs`

### フロントエンド

リポジトリルートで実行します。

```bash
cd frontend
npm run doctor
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
- 既存の SQLite ボリュームは自動移行されません。[移行 CLI と照合手順](sqlite-to-postgresql.md) に従って切り替えます。
- [トラック整理](track-compaction.md) は PostgreSQL の保持窓と参照整合性を確認してから実行します。

ローカルで migration-job を実行する場合は、backend の metadata を読み込めるようにします。
接続情報は `DB_HOST`、`DB_PORT`、`DB_NAME`、`DB_USER`、`DB_PASSWORD` で設定します。

```powershell
cd migration-job
$env:PYTHONPATH="../backend/src"
uv lock --check
uv sync --frozen
uv run --no-sync migration-job
uv run --no-sync migration-job-revision "add landing field"
```

revision コマンドは既存 head の次にファイルを作成します。テーブル変更は生成ファイルへ記述します。

## テスト

### バックエンド（pytest）

```bash
cd backend
uv run --no-sync python ../tools/dev.py check  # Ruff と全テスト
uv run --no-sync pytest tests/unit -q      # DB サーバー不要の単体テスト
uv run --no-sync pytest tests/unit/app/grading/test_land_grader.py -q   # 特定ファイル
```

- `asyncio_mode = "auto"` のため async テストはデコレータ不要
- フィクスチャ: `tests/fixtures/sample.acmi`、共通ヘルパーは `tests/conftest.py` / `tests/helpers.py`
- 共通DBフィクスチャを使うテストと圧縮処理のPostgreSQLテストは、PostgreSQLで実行します。`DLT_TEST_POSTGRES_URL` 未設定時は、共通fixtureがDockerで検証専用のPostgreSQL 18を自動起動し、接続先をテストセッションへ注入します。ローカル実行にはDockerの起動が必要です。サーバーを準備できない場合はskipせず、テストを失敗させます。
- rescan の保存・連鎖削除・同時実行・API 連携は `tests/integration/app/test_rescan.py` で PostgreSQL を使って検証します。
- PostgreSQL テストはテストごとに専用スキーマを作り、Alembic の head を適用して終了時に削除します。検証先に実運用 DB を指定しないでください。単体テストの一部は一時 SQLite を使います。
- 自動起動するサーバーは `127.0.0.1` の空きポートに公開し、セッション終了時にコンテナーと一時データを削除します。`tests/unit` のみの実行ではDBサーバーを起動しません。
- Windows の PostgreSQL 非同期テストと WebSocket クライアントは psycopg が対応する selector loop を使用します。

既に検証専用PostgreSQLを用意している場合は、次のように接続先を指定します。指定時はDockerでの自動起動を行いません。CIもこの方式でPostgreSQLサービスを利用します。

```powershell
cd backend
$env:DLT_TEST_POSTGRES_URL="postgresql+psycopg://<user>:<password>@localhost:5432/<test-db>"
uv run --no-sync pytest -q
```

migration-job 側でも同じ検証用 URL と `PYTHONPATH=../backend/src` を指定します。
`uv run --no-sync python ../tools/dev.py check` は Ruff・型検証の後、空 DB の upgrade/check/downgrade、再実行、既存データの補完、
意図した schema drift の検出を確認します。移行 CLI は次の独立したテストです。

```powershell
cd migration-job
uv run --no-sync pytest ../scripts/tests/test_migrate_sqlite.py -q
```

### フロントエンド（vitest）

```bash
cd frontend
npm test          # vitest run（1 回実行）
npm test -- --watch   # ウォッチモード
```

### Lint

```bash
cd backend
uv run --no-sync ruff check .
uv run --no-sync ruff check --fix .   # 自動修正可能な違反を修正
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
docker build -f docker/frontend/Dockerfile -t dlt-frontend .
```

## CI

`.github/workflows/ci.yml` が push / PR ごとに実行されます:

| ジョブ | 内容 |
|---|---|
| backend | Python 3.11.15 / uv 0.12.19 / PostgreSQL 18 / lock 鮮度確認 → frozen restore（source 実行）→ `--no-sync` の check |
| frontend | Node 22.23.3 / npm 11.9.0 / doctor → `npm ci` → `npm run check` |
| migration-job | 同じ Python/uv / PostgreSQL 18 / backend metadata / frozen project install → `--no-sync` の check（Ruff・basedpyright・migration・SQLite 移行 CLI） |
| compose | isolation guard → `docker compose config --quiet` → build → CI 専用 project の隔離起動・再作成 smoke |
| text-encoding | Windows/Linux の Git encoding、開発入口の失敗伝播・診断の privacy guard |

ローカルで CI と同じことを確認するには上記コマンドをそのまま実行してください。シークレットは不要です。

## コミット方針

- ブランチ: `main`（安定）＋ feature branch → Pull Request
- コミットメッセージ・PR 本文にローカルパスや個人名を含めないこと（公開リポジトリのため）
