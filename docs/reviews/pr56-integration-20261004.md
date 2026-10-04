# PR #56 の統合再レビュー

確認日: 2026-10-04 UTC。コードと隔離テストの記録であり、運用データの移行や本番切り替えの承認ではない。

## 統合したもの

- contributor の PR #56: `7fc4c686a5a97eec6b9460e42cd267764e95c236`
- 保守者側の接続設定・回帰テスト PR #70: `e1ef482d3c1696f1670c1a8cc2efb88bc280fbc8`
- current main の PR #69: `b6ff90770e9311817958cb4de424c5372a5d041e`
- 共通基点 `59b33d993689f2cfd67c9f5316c8d8cf0cc4c591` からの三方向比較では衝突なし。main が追加した2ファイルは、統合後も main とバイト一致する。
- #70 の231追跡ファイルを Git blob SHA と照合してから、独立した統合作業ツリーで検証した。
- contributor の fork / ブランチ、運用 DB、DCS / Tacview、認証情報は変更していない。

## 元レビューとの対応

| 指摘 | 現在の実装・証拠 | 判定 |
| --- | --- | --- |
| SQLite専用の0009とトリガー | migration-job の0009は LargeBinary と ON DELETE CASCADE。圧縮転送、削除連鎖、downgrade の PostgreSQL テストあり | 実装・隔離CIで対応 |
| PostgreSQL compact | backend/src/app/compact.py と test_compact_postgres.py。下見、バックアップ/停止確認、削除上限、ロック競合、参照確認、失敗時rollback | 実装・隔離CIで対応 |
| SQLite→PostgreSQL移行 | scripts/migrate_sqlite.py は入力読取専用。6テーブルの全列/ID、JSON null、boolean、UTC、BLOB、採番を保持。既存行のある移行先を拒否し、失敗・再実行を検証 | 実装・隔離CIで対応 |
| Alembic metadata/drift/再実行 | Base.metadataを参照。空DB→head、check、再実行、downgrade、0008補完、0009圧縮/復元のテストあり | 実装・隔離CIで対応 |
| 設定の黙った欠落 | ホスト優先、同梱フォールバック、WARNING、壊れたホスト設定では例外。seedのhost/bundled/exact優先順位を維持 | 実装・回帰テストで対応 |
| revision CLI/port/src import | main()がcreate_revisionを呼ぶ。EXPOSEとuvicornは9001。scriptsはbackend/srcを参照 | 対応 |
| Tacview既定・旧設定 | 42674に統一、31010の明示指定を維持。legacy/JSON双方の回帰テストあり | 対応 |
| runway cache永続性 | 名前付きvolumeを/dataに配置。新CI smokeはAPI再作成後のマーカー保持を検証 | 設定対応、新CI結果を参照 |
| Compose実起動とWebSocket | 元CIはconfig/buildのみ。新CI smokeでmigration→API→proxy起動、SPA、REST、WebSocket ping/pong | 新CIで補完 |
| 接続情報の予約文字 | #70でURL.createへ変更。新CI smokeも予約文字入りの合成パスワードを使い、古い.env URLより優先されることを確認 | 単体試験対応、新CI結果を参照 |
| ルート生成物除外 | Pythonキャッシュ、仮想環境、node_modules、tsbuildinfo、.ideaの除外を戻し、代表6パターンを確認 | 今回対応 |
| AGENTS方針変更 | 個人パス等の匿名化・送信前差分確認とWindows/BOM/改行確認だけを復元。限定的な文書読み込み、確認済み並行編集時のworktree、引継ぎ時中心のRESUME運用、PostgreSQLコマンドを維持 | 合意された最小差分で対応 |
| PRの3分割提案 | contributorは現PR維持を希望。今回も3本への再分割ではなく保守者側の統合候補 | 採用方針は未変更 |

元レビューの全指摘を今も未対応とは扱わない。一方、元レビューの CHANGES_REQUESTED をこの文書だけで解除しない。

## 実測結果

統合後のローカル再実行:

- frontend: build成功、15ファイル / 182 tests passed
- backend unit: 222 passed、Ruff passed
- migration-job: 12 passed / 7 skipped、Ruff / basedpyright passed
- SQLite移行: 2 passed / 4 skipped
- CI smoke安全ガード: unittest 4 passed、Ruff / 構文検査 passed
- git diff --check、3方向統合、ファイルSHA照合、生成物ignore確認: passed

ローカルにはDocker/PostgreSQLがないためDB依存のskipを成功として数えない。共有依存環境を使う際は PYTHONPATH=src:../backend/src で今回のソースを優先する。最初のmigration再実行は旧コピーのeditable importを読み、パス照合が1件失敗した。今回のソースへ固定して再実行し、上記結果を得た。

直前の #70 exact head の [GitHub CI](https://github.com/MasterMk2/DCSLandingTeacher/actions/runs/37232487232):

- Backend: 423 passed
- migration-job: 19 passed、SQLite→PostgreSQL: 6 passed
- Frontend、Compose config/build: 成功
- 新しいスタック起動・再作成smokeはこの時点では存在しなかった。統合候補のCI結果とは区別する。

## 新しいCompose smokeの境界

scripts/ci_compose_smoke.py はGitHub Actionsのrun/attemptに対応する固有project名とloopback待受を必須とし、既存.envやremote Docker contextがあれば起動前に停止する。実データ、認証値、外部サービスは使わない。ACMIは無効にし、片付けで削除するのは当該CI projectのコンテナ・volumeだけ。

検証するもの:
1. 合成の予約文字入りパスワードでPostgreSQL migrationとAPI起動
2. reverse proxy越しのSPA fallback、REST JSON、WebSocket ping/pong
3. APIとproxyの再作成後もrunway cache volumeのマーカーが残ること
4. 成否にかかわらず当該CI stackを片付けること

検証しないもの:
- 現在の本番データの完全性、実DCS/Tacview受信、運用者による画面操作
- 本番バックアップ復元、停止時間、実データ量での移行・compact時間
- PostgreSQL運用開始後の追加データをSQLiteへ戻す逆移行

## mainへの統合前に残す確認

- 新しいCompose smokeを含む統合候補のexact-head CI結果を確認する。
- AGENTSの最小差分を含む最終headで、元レビューの解除判断を記録する。
- 185ファイル規模の構成変更を現PRの単位で取り込むか、レビューで確定する。
- 本番切り替えは別作業とし、バックアップ/復元確認、停止窓、移行件数/代表画面の照合、切り戻し条件を運用者が確認する。

本番切り替えを行わずにコードのレビューとCI検証を完了させることはできる。運用未実施を理由に、通った隔離検証を未実施扱いへ戻さない。

## 公開後のCompose実測

統合head `8261c0320558dc66cbb662bcb1562d119272d52d` の [CI](https://github.com/MasterMk2/DCSLandingTeacher/actions/runs/37234436068) は全4 jobs成功。新smokeがmigration、予約文字入り合成パスワード、API/SPA、WebSocket ping/pong、再作成後cache保持まで実際に完了した。後続のAGENTS最小修正はアプリコードを変更していないが、その最終headのCIも別途確認する。
