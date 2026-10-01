# PostgreSQL の生航跡を整理する

`backend/src/app/compact.py` は、着陸を記録した機体と対象空母について着陸前後の保持時間窓を残し、静的オブジェクトの航跡は全件残します。その他の `tracks` を整理します。保持時間窓はアプリの `retention_window` と同じ計算です。着陸の再走査が必要なら、整理前に完了してください。削除後に時間窓の外の航跡を再生成することはできません。

## 実行前

1. 対象 DB と適用済み Alembic revision が `0009_landing_tracks` であることを確認します。接続先は `DLT_DATABASE_URL` で指定します。接続情報はコマンドライン引数、記録、コミットへ書かず、安全な環境変数管理を使います。
2. API と取り込みなど、対象 DB へ書き込むアプリを停止します。Compose なら `docker compose stop api` を実行します。DB 自体は起動したままにします。
3. 対象 DB の完全なバックアップを取得します。たとえば PostgreSQL クライアントから `pg_dump -Fc -f <backup.dump> -d <接続先>` を実行し、`pg_restore -l <backup.dump>` でアーカイブを読めることを確認します。可能なら別の空 DB への復元も検証します。復元手順とバックアップの保管先を記録します。
4. `backend/` から `DLT_GRADING_CONFIG_PATH=../config/grading.yaml` と `DLT_DATABASE_URL` を設定し、`uv run python -m app.compact` を実行します。表示された保持・削除予定件数と保持・削除対象 ID の先頭 10 件を確認します。想定外の件数なら実行せず、接続先、着陸レコード、設定、再走査結果を調べます。

## 整理

下見で確認した削除予定件数を上限 `N` として、`backend/` から次を実行します。

```powershell
uv run python -m app.compact --execute --backup-confirmed --application-stopped --max-delete N
```

`--backup-confirmed` と `--application-stopped` は運用者による確認です。ツールは実行直前に再集計し、上限超過、保持対象が 0 件になる削除、参照切れ、対象テーブルの不足、同時書き込みによるロック競合で停止します。削除と件数・参照確認は 1 トランザクションで行い、失敗時にはロールバックします。停止後の再実行は新しい下見から始めます。

完了後は出力された `verified: tracks=...` を下見時の保持件数と照合し、着陸詳細の進入軌跡と再評価・再構築が必要な代表例を確認してから API を再開します。復旧が必要な場合は API を停止したまま、保存したバックアップを別 DB に復元して整合性を確認し、接続先を切り替えます。元の DB を上書きする復旧は、その DB を保全してから行います。

削除だけでは通常、DB のファイル容量は OS に戻りません。PostgreSQL の通常の `VACUUM (ANALYZE) tracks` は領域を再利用可能にします。ファイルを縮小する必要がある場合の `VACUUM FULL tracks` は排他ロックと追加ディスク容量が必要なので、停止時間・空き容量を確認した別の保守作業として実施します。
