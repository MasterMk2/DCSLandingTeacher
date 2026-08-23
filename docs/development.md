# 開発ガイド

開発環境の構築とテスト実行手順。アーキテクチャの全体像は [`architecture.md`](architecture.md) を参照してください。

## 必要要件

| ツール | バージョン |
|---|---|
| Python | 3.11 以上 |
| Node.js | 20 以上（LTS 推奨） |
| Docker（任意） | Docker Desktop または Engine + Compose v2 |

## バックエンド

### 環境構築

```bash
cd backend
python -m venv .venv

# Windows (PowerShell)
.venv\Scripts\Activate.ps1
# Linux / macOS
source .venv/bin/activate

pip install -e ".[dev]"
```

リポジトリルートに `.env` を作成（省略可）:

```bash
cp .env.example .env
```

API 単体で動かす場合は ACMI 受信を無効化すると Tacview なしで起動できます:

```bash
# Windows (cmd)
set DLT_ACMI_ENABLED=false&& set DLT_GRADING_CONFIG_PATH=../config/grading.yaml&& uvicorn app.api.main:create_app --factory --port 8000
# Linux
DLT_ACMI_ENABLED=false DLT_GRADING_CONFIG_PATH=../config/grading.yaml uvicorn app.api.main:create_app --factory --port 8000
```

- 動作確認: `http://localhost:8000/api/health`
- OpenAPI: `http://localhost:8000/docs`

### フロントエンド

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

本番相当の確認（FastAPI 静的配信）:

```bash
cd frontend && npm run build    # frontend/dist を生成
# リポジトリルートからバックエンドを起動すると dist が自動配信される
```

## テスト

### バックエンド（pytest、83 テスト）

```bash
cd backend
pytest -q                 # 全テスト
pytest tests/test_grading.py -q   # 特定ファイル
```

- `asyncio_mode = "auto"` のため async テストはデコレータ不要
- フィクスチャ: `tests/fixtures/sample.acmi`、共通ヘルパーは `tests/conftest.py` / `tests/helpers.py`
- DB はテストごとに一時ディレクトリ上の SQLite を使用（実データに影響しない）

### フロントエンド（vitest）

```bash
cd frontend
npm test          # vitest run（1 回実行）
npm test -- --watch   # ウォッチモード
```

### Lint

```bash
cd backend
ruff check .
ruff check --fix .   # 自動修正可能な違反を修正
```

ルールセットは [`backend/pyproject.toml`](../backend/pyproject.toml) の `[tool.ruff.lint]`（最小構成: E4/E7/E9/F）で管理しています。

## Docker での検証

```bash
docker compose up --build     # ビルド + 起動
curl http://localhost:8000/api/health
docker compose down           # ボリューム dlt-data は保持される
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
| backend | Python 3.11 / `pip install -e ".[dev]"` → `ruff check .` → `pytest -q` |
| frontend | Node 20 / `npm ci` → `npm run build` → `npm test` |

ローカルで CI と同じことを確認するには上記コマンドをそのまま実行してください。シークレットは不要です。

## コミット方針

- ブランチ: `main`（安定）＋ feature branch → Pull Request
- コミットメッセージ・PR 本文にローカルパスや個人名を含めないこと（公開リポジトリのため）
