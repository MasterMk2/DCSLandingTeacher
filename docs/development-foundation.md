# 開発基盤の共通契約 0.1

[共通契約 0.1](https://github.com/MasterMk2/dev-knowledge/blob/bbe5bf3e/docs/17-%E9%96%8B%E7%99%BA%E5%9F%BA%E7%9B%A4%E3%81%AE%E5%85%B1%E9%80%9A%E5%A5%91%E7%B4%84.md) の役割を、このリポジトリの既存チェックに対応付けます。製品仕様や DB schema の変更は含みません。

## 固定するものと例外

| 対象 | 開発・CI・build の基準 | 管理ファイル |
|---|---|---|
| backend / migration-job | CPython 3.11.15、uv 0.12.19 | ルート `.python-version`、各 `pyproject.toml` の `tool.uv`、各 `uv.lock` |
| frontend | Node 22.23.3、npm 11.9.0 | `frontend/.nvmrc`、`.npmrc`、`package.json`、`package-lock.json` |
| 検証用 DB | PostgreSQL 18 | テスト fixture、CI service、Compose |

Python は migration-job の `==3.11.*` と既存の動作を保持します。backend の互換宣言 `>=3.11` は、全 Python 版で検証済みという意味ではありません。Node の互換宣言は 22.23.3 以上の 22 系で、doctor は実測した基準版との一致を確認します。Node 24 や依存一括更新はこの適用に含みません。

backend は source を直接実行するため `--no-install-project`、migration-job は CLI と import を使うため project 自体もインストールします。後者の editable build で使う Hatchling は従来どおり `build-system.requires` の管理下で、`uv.lock` だけでは build 用依存まで固定されません。この例外を解消する変更は別途検証します。

Docker は同じ Python/uv と Node/npm の版で build します。base OS、nginx、PostgreSQL、Actions の runner は digest では固定していません。ホスト Docker/Compose と標準ライブラリだけを使う CI 補助検証の Python は runner 側の管理です。製品の lock はこれらを再現するものではありません。

## doctor → sync → check

uv は [公式配布](https://github.com/astral-sh/uv/releases/tag/0.12.19)、Node は [公式 22.23.3 配布](https://nodejs.org/dist/v22.23.3/) から対象 OS/CPU に対応する版を準備してください。ユーザー全体の設定を変更する必要はありません。

backend の初回復元:

```bash
cd backend
uv --version                     # 0.12.19
uv python install --no-bin --no-registry  # ルートの 3.11.15。global executable/registry は変更しない
uv lock --check                  # manifest と lock の鮮度確認
uv sync --frozen --no-install-project
uv run --no-sync python ../tools/dev.py doctor
uv run --no-sync python ../tools/dev.py check
```

migration-job の初回復元:

```bash
cd migration-job
uv python install --no-bin --no-registry
uv lock --check
uv sync --frozen
# PYTHONPATH=../backend/src と DLT_TEST_POSTGRES_URL を検証専用 DB に設定
uv run --no-sync python ../tools/dev.py doctor
uv run --no-sync python ../tools/dev.py check
```

frontend の初回復元（`.nvmrc` の Node を先に選択）:

```bash
cd frontend
# npm 11.9.0 を専用 runtime、または任意の専用 prefix に導入して PATH を選択
npm run doctor
npm ci
npm run check
```

doctor は版・OS/CPU・必要ツールだけを出力し、環境変数一覧、個人パス、接続 URL を表示しません。Python の doctor は復元後の環境を確認します。`--frozen` は lock の鮮度を確認しないので、先に `uv lock --check` を実行します。復元後は `--no-sync` で実行し、通常チェック中の依存再解決・インストールを避けます。`UV_PROJECT_ENVIRONMENT` を使う場合も、この checkout 専用の環境を指定してください。
Python の保存先も分ける場合は `UV_PYTHON_INSTALL_DIR` を専用ディレクトリに指定します。npm の専用 prefix は `npm install --prefix <TOOLS_DIR> npm@11.9.0` で作成し、その `node_modules/.bin` と固定 Node の bin を PATH に追加できます。CI と Docker はそれぞれ一時 runner/container 内で npm を固定します。

| 役割 | 内容・適用条件 |
|---|---|
| doctor | 上記の版と必要ツールを確認。不一致は失敗 |
| sync | Python は lock 鮮度確認 → frozen restore、frontend は `npm ci` |
| check: backend | Ruff → 全 pytest。編集した Python の型確認は AGENTS の条件に従い別途実行 |
| check: migration-job | Ruff → basedpyright → 全 pytest → SQLite 移行 CLI テスト。`DLT_TEST_POSTGRES_URL` が必須 |
| check: frontend | Vitest → TypeScript + Vite build。既存の lint コマンドはない |
| 追加検証 | Compose isolation guard → config → 全 image build → 隔離起動・再作成 smoke を CI で実行 |
| build | `npm run build`、または `docker compose build`。check とは別に成果物を作る入口 |
| release-check | 自動入口は未実装。成果物の版・実 runtime・配布先を確認してから別途承認 |

check は実運用 DB、Tacview、実 ACMI を使いません。PostgreSQL は専用 DB/一時 schema、SQLite は一時ファイルを使います。backend の DB 自動起動条件、検証 URL の指定方法は [開発ガイド](development.md#テスト) を参照してください。migration の check は DB を指定しないと失敗し、DB テストの skip を成功扱いにしません。check に通常運用への migration 適用、通知送信、配布を含めません。

## 検証範囲・更新・戻し方

この契約の確認対象は Windows x64 の専用環境と Ubuntu CI の backend/frontend/migration、および Ubuntu CI の Compose です。Windows/Linux の Git encoding と入口 guard も CI で確認します。macOS、ARM、実機録画、実サーバーでの受入は未検証です。

manifest は意図、lock は解決した製品依存の版・取得先・hash を管理します。通常の復元・check 後に dependency ファイルが変わらないことを CI で確認します。依存更新は manifest と lock を同じ変更で扱い、runtime 更新だけの PR に混ぜません。

製品版は backend/frontend `0.3.0`、migration-job `0.1.0` のままです。契約版 `0.1` は製品版と別です。CI はテストと build/smoke のみで、image push、release publish、自動 deploy の経路はありません。

戻す場合は変更前 commit の manifest・lock・runtime 指定を組として戻し、独立した環境で frozen restore と同じチェックをやり直します。共有環境や既存 DB のデータを戻す手順ではありません。
