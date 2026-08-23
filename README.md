# DCS Landing Teacher

DCS World Dedicated Server 上で行われた着陸（陸上空港）／着艦（空母）を、
Tacview の ACMI データストリームから記録・評価し、ブラウザで振り返りできるようにするツールです。

- Tacview Realtime Telemetry（ACMI 2.2 Text / TCP）の受信・解析
- 着陸／着艦イベントの自動検出
- 米海軍式 LSO グレーディングによる空母着艦の自動評価
- 陸上着陸の簡易評価（グライドスロープ偏差・センターライン偏差等）
- Web UI での閲覧（GCA スコープ風ビュー、トップダウン軌跡、時系列チャート）

詳細な要件は `plans/requirements.md` を参照してください。

## セットアップ

### 必要要件

- Python 3.11 以上
- DCS World + Tacview（リアルタイムテレメトリ出力を有効化）

### バックエンド

```bash
cd backend
python -m venv .venv
# Windows (PowerShell)
.venv/Scripts/Activate.ps1
# Linux
source .venv/bin/activate

pip install -e ".[dev]"
cp ../.env.example ../.env   # 必要に応じて編集
uvicorn app.api.main:app --reload
```

`http://localhost:8000/api/health` にアクセスして動作を確認できます。

### Tacview 側の設定

DCS の Tacview アドオン設定でリアルタイムテレメトリ出力（既定 TCP ポート 31010）を有効にし、
`.env` の `DLT_TACVIEW_HOST` / `DLT_TACVIEW_PORT` を合わせてください。

## ライセンス

MIT License。詳細は [LICENSE](LICENSE) を参照してください。
フライトデータはユーザー自身の所有物であり、サーバー管理者の責任で扱ってください。
