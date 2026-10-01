# SQLite から PostgreSQL へのデータ移行

対象ツールは `scripts/migrate_sqlite.py`、スクリプト版は `1`。この版は
`landing_tracks` を分離した旧 SQLite スキーマ（0009 相当）を対象とする。
旧スキーマの場合は、先に旧アプリケーションの移行手順で 0009 まで更新する。
このツールはスキーマの異なる SQLite を自動変換しない。

## 移行するデータ

| テーブル | 主キー | 保持する外部キー |
| --- | --- | --- |
| `flights` | `id` | なし |
| `objects` | `id` | `flight_id` → `flights.id` |
| `tracks` | `id` | `flight_id` → `flights.id`、`object_id` → `objects.id` |
| `landings` | `id` | `flight_id` → `flights.id`、`object_id` と `carrier_object_id` → `objects.id` |
| `landing_tracks` | `landing_id` | `landing_id` → `landings.id` |
| `import_jobs` | 文字列 `id` | なし |

全列と元の ID を保持する。JSON の TEXT は PostgreSQL の JSON に、0/1 は
boolean に変換する。日時のタイムゾーンが欠けている場合は UTC と解釈し、
指定済みの場合はその時刻を保持する。SQL NULL と JSON null は区別する。
`landing_tracks.approach_track` は圧縮済み BLOB をそのまま bytea に移す。
`flights`、`objects`、`tracks`、`landings` のシーケンスを各テーブルの最大 ID に
合わせ、次の採番が既存 ID より先になるようにする。

## 準備と実行

1. SQLite を使用する API、ACMI 受信、インポート、および保守処理を停止する。
   PostgreSQL 側も移行完了までアプリケーションを起動しない。
2. 停止状態で SQLite のバックアップを取得する。WAL がある場合は本体だけを
   コピーせず、SQLite のオンラインバックアップ API で一貫したコピーを作る。
   元 DB とバックアップを保全し、以降はコピーを移行入力にする。
3. 移行専用の空 PostgreSQL DB と、対象スキーマで DDL・INSERT・シーケンス更新を
   実行できるユーザーを用意する。接続先を確認し、共有の運用 DB を指定しない。
4. リポジトリの `migration-job/` で `uv sync --frozen --no-install-project` を実行する。
   この環境には CLI が使用する Alembic、SQLAlchemy、psycopg が含まれる。
5. 同じディレクトリから次を実行する。接続 URL は保護された環境変数
   `DLT_DATABASE_URL` に設定する。URL、パスワード、実データを履歴・文書・Git に残さない。

```powershell
uv run python ../scripts/migrate_sqlite.py --version
uv run python ../scripts/migrate_sqlite.py <SQLiteバックアップのパス>
```

CLI は `backend/src` を読み込むため、手動の `PYTHONPATH` 設定は不要。
接続 URL の形式は `postgresql+psycopg://...`。任意のスキーマを利用する場合は
接続の `search_path` を固定し、全手順で同じスキーマを使う。

ツールは入力を読み取り専用で開き、SQLite の整合性と外部キーを検査する。
対象テーブルが空であることを確認した後、PostgreSQL に `upgrade head` と
`alembic check` を実行する。`stamp` は実行しない。全列の対応を確認してから
単一トランザクションで投入し、全テーブルの行数、外部キー参照、Alembic head を
確認する。投入中は対象テーブルへの他の書き込みをロックする。

## 完了確認と切り替え

正常終了では版番号と `verified_rows` の JSON が出力される。次を確認する。

- 六つのテーブルの件数を SQLite の `SELECT count(*)` と照合する。
- `landings` の機体・空母参照、`landing_tracks` の着陸参照を確認する。
  ツール内でも全外部キーに対する孤立レコードの検査を実行する。
- PostgreSQL の `alembic_version` が当該コードの head と一致することを確認する。
- 新しいアプリケーションで着陸一覧、圧縮トラックの表示、インポート履歴を確認する。

照合が終わってから PostgreSQL 向け設定でアプリケーションを起動する。
SQLite とバックアップは、移行完了を確認し保管方針に従って処分するまで保持する。

## 失敗・再実行・切り戻し

失敗時は入力を変更しない。投入トランザクションはロールバックされるが、先に
適用した PostgreSQL スキーマは残る場合がある。出力には接続情報や入力値を含めない。
まずアプリケーション停止と移行先を再確認し、六つのテーブルが空であることを
照合する。シーケンス更新は PostgreSQL の非トランザクション操作であるため、
障害時に値が進んでいることがある。再実行が成功すれば最大 ID に再設定される。

入力の版・列・JSON・boolean・参照関係に問題がある場合は、保全済みバックアップを
残し、別のコピー上で旧アプリケーションの手順に従って修復する。原因を解消してから
同じ空の移行先へ再実行できる。既存の application 行がある移行先は拒否され、
追記・上書き・重複投入は行わない。部分的な手動投入や外部からの書き込みがある場合は、
その内容を確認・保全してから、新しい空の移行先を用意する。

移行前または切り替え直後に戻す場合は、新アプリケーションを停止し、保全した
SQLite を旧アプリケーションで利用する。PostgreSQL で新規の受信・インポートが
始まった後は、その追加データが SQLite に存在しないため、単純に切り戻さず
追加分の保全と取り込み方針を決める。

## 検証

機密情報のない fixture を使用する。`migration-job/` から
`DLT_TEST_POSTGRES_URL` に検証専用 PostgreSQL を指定して実行する。
各テストは固有スキーマを作り、終了時にそのスキーマだけを削除する。

```powershell
uv run pytest ../scripts/tests/test_migrate_sqlite.py -q
```

検証対象は CLI の実行、全型変換、全テーブルの件数・参照、ID 継続、Alembic head、
入力不変性、既存行がある移行先の拒否、および投入失敗後の空状態と再実行。
