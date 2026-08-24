# Issue 起票候補（バックログ）

要件定義書 §10 の未決事項と、実装タスクで判明した課題を整理したものです。
GitHub Issue として起票する際のたねリストとして利用してください。
起票時は要件定義書の ID（O-nn）をタイトルや本文に残し、トレース可能にしてください。

## 検証

### O-1: 実データでの空母 Type コード・空港 Static オブジェクト検証
- **種別**: 検証 / ラベル: `verification`, `requirements`
- 実際の DCS マルチプレイ環境の ACMI ストリームで、空母 `Type=Carrier+...` の
  艦種コード網羅性と、空港が Static object としてどう表現されるかを確認する
- 合成テストデータ（`tests/fixtures/sample.acmi`）では確認済みだが実データ未検証
- 受け入れ条件: 主要艦（Kuznetsov / Stennis / Forrestal 等）＋複数マップの空港で着陸検出が機能する

### O-6: Tacview リアルタイムストリームの遅延・圧縮対応範囲
- **種別**: 検証 / ラベル: `verification`, `enhancement`
- Realtime Telemetry の遅延特性の計測。`.zip` 圧縮 ACMI（ファイル転送モード）への対応要否
- リアルタイム運用では Text モードのみ対応済み

## 幾何・評価精度

### O-2: FLOLS グライドスロープの厳密な幾何
- **種別**: 調査 / ラベル: `accuracy`, `grading`
- 艦ごとのランプ位置・甲板高度を実測値ベースで精緻化する（現在は `config/grading.yaml` の
  `carrier_glideslope_deg: 3.5` と近接判定距離での近似）
- 受け入れ条件: 艦種ごとの幾何定義を config に追加し、グレードの一貫性を検証

### O-3: BURBLE 等の環境ファクターの検出精度
- **種別**: 調査 / ラベル: `grading`, `investigation`
- 甲板風（BURBLE）の影響検出に必要な風データが ACMI で取得可能か調査
- 取得不可なら簡易版（高度変動パターンからの推定）の精度を検証するか、スコープ外と明記

### full_stop 確定遅延の調整
- **種別**: 改善 / ラベル: `detection`, `ux`
- full-stop 判定は `full_stop_dwell_s`（既定 15 秒）の滞地待ちが必要なため、
  着陸直後の UI 反映が最大 15 秒遅れる。中間状態（touchdown 確定・結果未確定）を
  先行通知する案あり

## 設計

### O-5: 複数同時着艦（トラフィック混在時の進入区間切り分け）
- **種別**: 設計 / ラベル: `design`, `detection`
- 複数機が同時に最終進入している場合、進入区間のサンプル切り出しが機体単位で
  正しく分離できるか検証・設計する（現在は機体別バッファで分離。同一機の連続進入が主な懸念）

### DB マイグレーション導入
- **種別**: 設計 / ラベル: `infrastructure`, `database`
- 現状は `init_db` による CREATE TABLE のみ。スキーマ変更時に既存 SQLite データが
  保持できないため、Alembic 等のマイグレーション導入を検討する
- 受け入れ条件: スキーマ変更後も旧 DB ファイルが自動アップグレードされる

## 要件確認

### O-4: 認証機能の要否
- **種別**: 要件確認 / ラベル: `question`, `security`
- 公開サーバー（インターネット露出）で運用する場合の認証・アクセス制御の要否
- 初版は LAN 内単一サーバー運用を想定しており認証なし

### O-7: データシートのフォーマット
- **種別**: 要件確認 / ラベル: `question`, `frontend`
- FR-6 データシート出力の実現方式: HTML 印刷（CSS print）か PDF 直接生成か
- CSV エクスポートは実装済み（`frontend/src/lib/csv.ts`）。データシート（GCA スコープ図含む）は未実装

## 実データ検証で判明した不具合（2026-08 デバッグセッション）

> **対応状況（2026-08 デバッグセッション第2弾）**: D-1 / D-4 / D-5 は実装・
> テスト済み。D-2 は位置微分フォールバックまで実装（単位検証は残課題）。
> D-3 はスロットリング実装済み（1/5 の受入基準は実測未達成確認）。
> いずれもコミット前のワーキングツリー状態。

### D-1: 表示日時がゲーム内時間になる（実時間と一致しない）
- **状態**: 対応済み — `LandingSummary` / `LandingDetail` に `touchdown_epoch`
  （`Flight.reference_time` + `touchdown_time`）を追加し、`LandingTable.tsx` /
  `Detail.tsx` / CSV 出力で使用。回帰テスト込み。
- **種別**: 不具合 / ラベル: `bug`, `frontend`, `ux`
- ダッシュボード・詳細画面の「日時」が ACMI のミッション内経過秒
  （`touchdown_time`）をそのまま epoch 秒として `formatEpoch()` に渡しているため、
  1970 年起点の未来日時（例: 1970/01/01 09:47）として表示される
- 正しくは `Flight.reference_time`（ACMI ヘッダの `ReferenceTime`、例
  `2011-06-02T05:00:00Z`）＋ `touchdown_time` を表示用 epoch に合成する必要がある
- 対応案:
  - バックエンド: `LandingSummary` / `LandingDetail` に
    `touchdown_epoch`（reference_time + touchdown_time）を追加するか、
    `flight_reference_time` をレスポンスに含めてフロントで合成
  - フロント: `formatEpoch(touchdown_time)` の呼び出し箇所
    （`LandingTable.tsx`, `Detail.tsx`）を合成済み値に差し替え
- 受け入れ条件: 実データ取り込み後、着陸日時がミッション開始の実時刻と整合する

### D-2: Speed 計算がすべて失敗する（速度が常に null / "-"）
- **状態**: 部分対応 — プロパティ欠落時の haversine 対地速度フォールバックを
  `TrackIngestor._derived_ground_speed()` として実装済み。
  **残課題**: TAS/CAS/IAS の単位検証（kt 出力の可能性）。実ストリームの生
  プロパティをダンプして確定するまで、素の値の kt→m/s 変換は入れない。
- **種別**: 不具合 / ラベル: `bug`, `grading`
- タッチダウン速度・進入速度比（FAST/SLOW 判定）が実データで全件失敗する
- 原因候補（要ログ確認）:
  1. DCS Tacview エクスポータは `TAS`/`CAS`/`IAS` を m/s ではなく kt で
     出力するケースがあり、単位変換が未実装
  2. 一部機体では速度プロパティ自体が出力されず、`AcmiObject.speed` が
     常に None になる（位置微分によるフォールバック未実装）
- 対応案:
  - 単位検証: 実ストリームの生プロパティをダンプして単位を確定させる
  - フォールバック: `speed` が欠落している場合、連続サンプルの
    haversine 距離 / dt から対地速度を導出する（`TrackSample.speed` 生成時に適用）
- 受け入れ条件: 実データの着陸で `touchdown_speed_ms` が妥当な値
  （着艦なら 130–160 kt 相当）になり、FAST/SLOW が判定される

### D-3: 解析（インポート）に異常に長い時間がかかる
- **状態**: 対応済み（効果の実測は未実施）— `ANALYSIS_INTERVAL_S = 1.0` による
  機体ごとの解析間隔制限、`DETECTION_AGL_GATE_M = 150` による高高度スキップ、
  `GROUND_ALT_CACHE_S = 5.0` による地面高度参照キャッシュ、強制 flush を
  着陸イベント検出時のみに限定、を実装。「現状の 1/5」の受入基準は実データで計測して確認する必要あり。
- **種別**: 改善 / ラベル: `performance`, `import`
- 数十分規模の ACMI ファイルのインポートが体感で「ありえないほど」遅い。
  現在の実装は行ごとに以下を実行しており O(N) 行あたりのコストが高い:
  - `_handle_update`: オブジェクト初回のみとはいえ都度 `session.get` /
    SELECT が走る可能性
  - `_maybe_detect_landing`: **航空機の全更新ごとに** `analyze_track` を
    バッファ全体（最大 600 秒分）へ再実行 + 強制 flush（commit）
  - `_flush(force=True)` が SQLite 単一ライタのコミットを大量発生させる
- 対応案:
  - 検出のトリガーを絞る: AGL/on_ground 変化や降下率が閾値近い場合のみ
    `analyze_track` を実行（フル再解析は接地候補時のみ）
  - 強制 commit は「着陸イベント検出時のみ」に限定し、通常更新では
    バッチコミットに戻す
  - インポート完了までの進捗表示（frames_processed は既存）に加え、
    処理レート（lines/s）をジョブステータスに追加して計測可能にする
- 受け入れ条件: 30 分相当の ACMI が現状の 1/5 以下の時間で解析完了する

### D-4: 距離・偏差系の単位をメートルから ft/nm に統一する
- **状態**: 対応済み — `format.ts` に `mToFt()` / `mToNm()` を追加し、GCA スコープ
  リング・偏差軸・CSV ヘッダ（`*_ft` / `*_nm` / `speed_kt` / `agl_ft`）・接地点
  高度表示を変換。内部 API はメートル維持。フロントテスト更新済み。
- **種別**: 改善 / ラベル: `enhancement`, `frontend`, `ux`
- 航空運用の慣例に合わせ、距離は nm、高度・偏差は ft で表示する
  （内部計算は SI のまま、表示層のみ変換）
- 対象:
  - GCA スコープの距離リング・ラベル（現在 km 表記）→ nm
  - 偏差軸ラベル（現在 m）→ ft
  - CSV エクスポートのヘッダ（`distance_to_go_m` 等）→ `_ft` / `_nm` 列追加か切替
  - 接地点情報の高度表示 → ft
- 内部 API（`DeviationSampleOut` 等）はメートル維持とし、フロントの
  `format.ts` に `mToFt()` / `mToNm()` を追加して変換する方針
- 受け入れ条件: UI 上の距離・高度・偏差がすべて nm/ft 表記になり、
  既存テストが更新される

### D-5: 陸上着陸の降下パス基準を 3 度に統一
- **状態**: 対応済み — `config/grading.yaml` の `land_glideslope_deg: 3.0` を仕様と
  して明文化（空母 FLOLS 3.5 度は維持）。`glideslope_for(kind)` の経路と既定値を
  検証する回帰テストを追加済み。
- **種別**: 改善 / ラベル: `grading`, `config`
- `config/grading.yaml` の `land_glideslope_deg` は既定 3.0 だが、
  実データでの評価基準として「地上の場合は 3 度を基準」と明文化・固定したい
- 対応:
  - `land_glideslope_deg: 3.0` を仕様として README / 要件定義書に明記
  - regrade API のオーバーライドで変更可能だが、デフォルトは 3.0 を保証
  - （空母側は FLOLS 幾何 `carrier_glideslope_deg: 3.5` / carriers.yaml の
    艦別値を維持。O-2 の実測精緻化は継続課題）
- 受け入れ条件: 陸上着陸の glideslope 偏差が 3 度基準で計算されていることを
  テストで担保する

## その他（実装タスク由来）

### README スクリーンショットの差し替え
- **種別**: ドキュメント / ラベル: `documentation`
- README のスクリーンショット枠（`docs/images/*.png` プレースホルダ）に実際の画面を配置する

### WebSocket パスの表記ゆれ解消（対応済み: Issue #11）
- **種別**: ドキュメント / ラベル: `documentation`
- ドキュメント・コメント内の WebSocket パス表記を実際のパス `/api/ws/landings` に統一した
  （要件定義書 plans/requirements.md 自体には該当表記がなく、履歴資料として変更不要と判断）
