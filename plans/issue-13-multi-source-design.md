# Issue #13: 複数 Tacview ソースの同時購読 - 設計ドキュメント

## 概要
DCSWebGCA の `sources[]` に相当する設計を導入し、1プロセスで複数の DCS サーバ（Tacview Real-Time Telemetry 接続先）を同時に購読できるようにする。

## 現状の制約
- `backend/app/config.py` の `Settings` に `tacview_host` / `tacview_port` のみ
- 1プロセス = 1 DCS サーバのみ
- 複数サーバ運用ではコンテナ・DB・ビルド一式をサーバ台数分用意必要

## 設計目標
- 設定: 複数ソースの配列 (`id`, `name`, `host`, `port`, `password?`) を受け付ける
- Ingest: ソースごとに独立した ACMI 接続を張り、フレームにソース ID をタグ付け
- DB スキーマ: `flights` と `landings` にソース識別カラムを追加
- REST API: 既存フィルタ同様に `source` でのフィルタを追加
- フロントエンド: ソース切り替え UI (ダッシュボード / GCA スコープ風ビュー両方)
- 後方互換: 単一ソース設定 (`DLT_TACVIEW_HOST`/`PORT`) をソース 1 件のデフォルトとして扱う

---

## 1. 設定設計 (`backend/app/config.py`)

### 1.1 新しい設定構造

```python
from pydantic import BaseModel, Field
from typing import Optional

class TacviewSource(BaseModel):
    """単一の Tacview 接続ソース設定"""
    id: str = Field(..., description="一意のソース識別子 (例: 'server1', 'caucasus-main')")
    name: str = Field(..., description="表示用名称 (例: 'Caucasus Main', 'NTTR Training')")
    host: str = Field(default="127.0.0.1", description="Tacview サーバのホスト/IP")
    port: int = Field(default=31010, description="Tacview サーバのポート")
    password: str = Field(default="", description="ハンドシェイク用パスワード (未使用時は空)")
    client_name: str = Field(default="DCSLandingTeacher", description="クライアント名")
    idle_timeout: float = Field(default=60.0, description="アイドルタイムアウト秒 (0で無効)")
    enabled: bool = Field(default=True, description="このソースを有効にするか")

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DLT_", env_file=".env", extra="ignore")
    
    # ... 既存設定 ...
    
    # Tacview 接続ソース (複数対応)
    # JSON 文字列として環境変数で受け取る: DLT_TACVIEW_SOURCES='[{"id":"s1","name":"Main","host":"127.0.0.1","port":31010}]'
    tacview_sources_json: str = Field(
        default="",
        alias="tacview_sources",
        description="Tacview ソース配列の JSON 文字列"
    )
    
    # 後方互換: 単一ソース設定 (tacview_sources_json が空の場合に使用)
    tacview_host: str = "127.0.0.1"
    tacview_port: int = 31010
    tacview_client_name: str = "DCSLandingTeacher"
    tacview_password: str = ""
    acmi_idle_timeout: float = 60.0  # 新規追加: デフォルトアイドルタイムアウト
    
    @property
    def tacview_sources(self) -> list[TacviewSource]:
        """解析済みの Tacview ソース一覧を返す"""
        if self.tacview_sources_json:
            import json
            data = json.loads(self.tacview_sources_json)
            return [TacviewSource(**item) for item in data]
        # 後方互換: 単一ソース設定からデフォルトソースを構築
        return [TacviewSource(
            id="default",
            name="Default",
            host=self.tacview_host,
            port=self.tacview_port,
            password=self.tacview_password,
            client_name=self.tacview_client_name,
            idle_timeout=self.acmi_idle_timeout,
        )]
    
    @property
    def tacview_enabled(self) -> bool:
        """少なくとも1つのソースが有効かどうか"""
        return any(s.enabled for s in self.tacview_sources)
```

### 1.2 環境変数例

```bash
# 単一ソース (従来互換)
DLT_TACVIEW_HOST=192.168.1.100
DLT_TACVIEW_PORT=31010

# 複数ソース (新方式) - JSON で指定
DLT_TACVIEW_SOURCES='[
  {"id": "caucasus-main", "name": "Caucasus Main", "host": "192.168.1.100", "port": 31010, "password": "secret1"},
  {"id": "nttr-training", "name": "NTTR Training", "host": "192.168.1.101", "port": 31010, "password": "secret2"},
  {"id": "persian-gulf", "name": "Persian Gulf", "host": "192.168.1.102", "port": 31010}
]'
```

---

## 2. データベーススキーマ変更

### 2.1 マイグレーション: `source_id` カラム追加

```python
# backend/migrations/versions/0003_multi_source_support.py
"""Add source_id to flights and landings for multi-source support."""

from alembic import op
import sqlalchemy as sa

def upgrade():
    # flights テーブルに source_id 追加
    op.add_column("flights", sa.Column("source_id", sa.String(64), nullable=True, index=True))
    op.create_index("ix_flights_source_id", "flights", ["source_id"])
    
    # landings テーブルに source_id 追加 (flights 経由で参照可能だが、クエリ効率化のため直接持つ)
    op.add_column("landings", sa.Column("source_id", sa.String(64), nullable=True, index=True))
    op.create_index("ix_landings_source_id", "landings", ["source_id"])
    
    # 既存データの source_id を 'default' で埋める
    op.execute("UPDATE flights SET source_id = 'default' WHERE source_id IS NULL")
    op.execute("UPDATE landings SET source_id = 'default' WHERE source_id IS NULL")
    
    # NOT NULL 制約追加 (将来的に)
    # op.alter_column("flights", "source_id", nullable=False)
    # op.alter_column("landings", "source_id", nullable=False)

def downgrade():
    op.drop_index("ix_landings_source_id", table_name="landings")
    op.drop_column("landings", "source_id")
    op.drop_index("ix_flights_source_id", table_name="flights")
    op.drop_column("flights", "source_id")
```

### 2.2 エンティティ変更 (`backend/app/models/entities.py`)

```python
class Flight(Base):
    __tablename__ = "flights"
    
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(64), index=True, default="default")  # 新規
    reference_time: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # ... 既存フィールド ...

class Landing(Base):
    __tablename__ = "landings"
    
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    flight_id: Mapped[int] = mapped_column(ForeignKey("flights.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[str] = mapped_column(String(64), index=True, default="default")  # 新規
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.id", ondelete="CASCADE"), index=True)
    # ... 既存フィールド ...
```

---

## 3. バックエンド Ingest 設計

### 3.1 MultiSourceAcmiManager (新規クラス)

```python
# backend/app/acmi/multi_source.py (新規ファイル)
"""複数 Tacview ソースの同時接続管理"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from logging import getLogger
from typing import Any

from app.acmi.stream import AcmiStreamClient
from app.config import TacviewSource, get_settings
from app.ingest import TrackIngestor, LandingListener, LandingFinalizeListener

logger = getLogger(__name__)


@dataclass
class SourceContext:
    """ソースごとの実行時コンテキスト"""
    source: TacviewSource
    client: AcmiStreamClient
    ingestor: TrackIngestor
    task: asyncio.Task | None = None
    connected: bool = False


class MultiSourceAcmiManager:
    """複数の Tacview ソースを並行して管理"""
    
    def __init__(
        self,
        session_factory,
        landing_listener: LandingListener | None = None,
        landing_finalize_listener: LandingFinalizeListener | None = None,
    ):
        self._session_factory = session_factory
        self._landing_listener = landing_listener
        self._landing_finalize_listener = landing_finalize_listener
        self._sources: dict[str, SourceContext] = {}
        self._running = False
    
    async def start(self) -> None:
        """全有効ソースの接続を開始"""
        settings = get_settings()
        self._running = True
        
        for source in settings.tacview_sources:
            if not source.enabled:
                logger.info("Skipping disabled source: %s (%s)", source.name, source.id)
                continue
            
            await self._start_source(source)
    
    async def _start_source(self, source: TacviewSource) -> None:
        """単一ソースの接続・ingest パイプラインを開始"""
        # ソースごとの ingestor 作成 (source_id を渡す)
        ingestor = TrackIngestor(
            self._session_factory,
            landing_listener=self._landing_listener,
            landing_finalize_listener=self._landing_finalize_listener,
        )
        # source_id を ingestor に設定 (後で _ensure_flight で使用)
        ingestor.source_id = source.id
        
        # コールバックで source_id をタグ付け
        async def on_line_with_source(line: str) -> None:
            await ingestor.handle_line(line)
        
        client = AcmiStreamClient(
            host=source.host,
            port=source.port,
            on_line=on_line_with_source,
            client_name=source.client_name,
            password=source.password,
            idle_timeout=source.idle_timeout,
        )
        
        ctx = SourceContext(
            source=source,
            client=client,
            ingestor=ingestor,
        )
        ctx.task = asyncio.create_task(self._run_source(ctx))
        self._sources[source.id] = ctx
        logger.info("Started ACMI client for source: %s (%s:%d)", source.name, source.host, source.port)
    
    async def _run_source(self, ctx: SourceContext) -> None:
        """ソースごとの run ループ (再接続含む)"""
        while self._running:
            try:
                await ctx.client.run()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Source %s client error: %s", ctx.source.id, exc)
            if not self._running:
                break
            # 再接続前の待機 (backoff は client 側で処理)
            await asyncio.sleep(1.0)
        ctx.connected = False
    
    async def stop(self) -> None:
        """全ソースを停止"""
        self._running = False
        for ctx in self._sources.values():
            await ctx.client.stop()
            if ctx.task:
                ctx.task.cancel()
                try:
                    await ctx.task
                except asyncio.CancelledError:
                    pass
            await ctx.ingestor.close()
        self._sources.clear()
    
    def get_source_status(self) -> list[dict[str, Any]]:
        """ヘルスチェック/監視用のステータス取得"""
        return [
            {
                "id": ctx.source.id,
                "name": ctx.source.name,
                "host": ctx.source.host,
                "port": ctx.source.port,
                "connected": ctx.connected,
                "enabled": ctx.source.enabled,
            }
            for ctx in self._sources.values()
        ]
```

### 3.2 TrackIngestor への source_id 統合

```python
# backend/app/ingest.py の変更点

class TrackIngestor:
    def __init__(self, ..., source_id: str = "default"):
        # ...
        self._source_id = source_id
    
    async def _ensure_flight(self) -> int:
        if self._flight_id is not None:
            return self._flight_id
        
        header = self._parser.header
        session = self._get_session()
        flight = Flight(
            source_id=self._source_id,  # 新規: ソースID記録
            reference_time=header.get("ReferenceTime"),
            # ... 既存フィールド ...
        )
        # ...
```

---

## 4. API 設計変更

### 4.1 スキーマ更新 (`backend/app/api/schemas.py`)

```python
class LandingSummary(BaseModel):
    # ... 既存フィールド ...
    source_id: str | None = None  # 新規
    source_name: str | None = None  # 新規: 表示用

class LandingListResponse(BaseModel):
    items: list[LandingSummary]
    total: int
    limit: int
    offset: int
    sources: list[SourceInfo] | None = None  # 新規: 利用可能ソース一覧

class SourceInfo(BaseModel):
    id: str
    name: str
    connected: bool

class LandingFilters(BaseModel):
    # ... 既存フィルタ ...
    source: str | None = Query(default=None, description="Filter by source ID")
```

### 4.2 ルート更新 (`backend/app/api/routes.py`)

```python
@protected_router.get("/landings", response_model=LandingListResponse)
async def list_landings(
    request: Request,
    source: str | None = Query(default=None, description="Filter by source ID"),
    # ... 既存パラメータ ...
    session: AsyncSession = Depends(get_session),
) -> LandingListResponse:
    query = (
        select(Landing, DcsObject, Flight)
        .join(DcsObject, Landing.object_id == DcsObject.id, isouter=True)
        .join(Flight, Landing.flight_id == Flight.id, isouter=True)
        .order_by(...)
    )
    
    if source:
        query = query.where(Landing.source_id == source)
    
    # ... 既存フィルタ処理 ...
    
    # 利用可能ソース一覧も返す (フロントエンドのセレクタ用)
    sources_result = await session.execute(
        select(Flight.source_id.distinct(), Flight.source_id)
        .where(Flight.source_id.is_not(None))
    )
    sources = [
        {"id": row[0], "name": row[0], "connected": True}  # connected は別途取得
        for row in sources_result.all()
    ]
    
    return LandingListResponse(
        items=items, total=total, limit=limit, offset=offset, sources=sources
    )
```

### 4.3 ヘルスエンドポイント拡張

```python
@router.get("/health")
async def health(request: Request) -> dict:
    # ... 既存 ...
    manager = getattr(request.app.state, "multi_source_manager", None)
    sources_status = manager.get_source_status() if manager else []
    return {
        "status": "ok",
        "acmi_sources": sources_status,  # 新規
        # ...
    }
```

---

## 5. フロントエンド設計

### 5.1 型定義更新 (`frontend/src/types/api.ts`)

```typescript
export interface LandingSummary {
  // ... 既存 ...
  source_id?: string | null;
  source_name?: string | null;
}

export interface SourceInfo {
  id: string;
  name: string;
  connected: boolean;
}

export interface LandingListResponse {
  items: LandingSummary[];
  total: number;
  limit: number;
  offset: number;
  sources?: SourceInfo[];
}

export interface LandingFilters {
  // ... 既存 ...
  source?: string;
}
```

### 5.2 ソースセレクタコンポーネント (新規: `frontend/src/components/SourceSelector.tsx`)

```tsx
import { useMemo } from 'react';
import { SourceInfo } from '../types/api';

interface SourceSelectorProps {
  sources: SourceInfo[];
  currentSource: string | null;
  onChange: (sourceId: string | null) => void;
}

export const SourceSelector: React.FC<SourceSelectorProps> = ({
  sources,
  currentSource,
  onChange,
}) => {
  const allSources = useMemo(() => [
    { id: null, name: 'すべてのソース', connected: true },
    ...sources,
  ], [sources]);

  return (
    <select
      value={currentSource ?? ''}
      onChange={(e) => onChange(e.target.value || null)}
      className="source-selector"
    >
      {allSources.map((src) => (
        <option key={src.id ?? 'all'} value={src.id ?? ''}>
          {src.name} {src.connected ? '🟢' : '🔴'}
        </option>
      ))}
    </select>
  );
};
```

### 5.3 ダッシュボード統合 (`frontend/src/views/Dashboard.tsx`)

```tsx
// FilterBar にソースフィルタ追加
// LandingTable に source_name 列表示オプション追加
// 詳細ビューにも source_name 表示
```

### 5.4 フック更新 (`frontend/src/hooks/useLandings.ts`)

```typescript
// source パラメータを API 呼び出しに追加
// ソース一覧取得用のフック追加
```

---

## 6. アプリケーション起動時の統合 (`backend/app/api/main.py`)

```python
# backend/app/api/main.py の変更

from app.acmi.multi_source import MultiSourceAcmiManager

# lifespan 内
if settings.acmi_enabled:
    manager = MultiSourceAcmiManager(
        session_factory=session_factory,
        landing_listener=pipeline.handle_landing,
        landing_finalize_listener=pipeline.finalize_landing,
    )
    await manager.start()
    app.state.multi_source_manager = manager
    # 既存の単一 client は互換のため残す (または削除)
    app.state.acmi_client = list(manager._sources.values())[0].client if manager._sources else None
else:
    manager = None

# shutdown 時
if manager:
    await manager.stop()
```

---

## 7. 実装順序とタスク分解

### Phase 1: バックエンド基盤 (優先度: 高)
1. [ ] `backend/app/config.py` - `TacviewSource` モデルと `tacview_sources` プロパティ追加
2. [ ] マイグレーション作成・実行 (`0003_multi_source_support.py`)
3. [ ] `backend/app/models/entities.py` - `Flight` と `Landing` に `source_id` 追加
4. [ ] `backend/app/ingest.py` - `TrackIngestor` に `source_id` 対応
5. [ ] `backend/app/acmi/multi_source.py` - `MultiSourceAcmiManager` 新規作成
6. [ ] `backend/app/api/main.py` - 起動時のマルチソースマネージャ初期化

### Phase 2: API・スキーマ (優先度: 高)
7. [ ] `backend/app/api/schemas.py` - `source_id`, `source_name`, `SourceInfo` 追加
8. [ ] `backend/app/api/routes.py` - `/landings` に `source` フィルタ、ソース一覧返却
9. [ ] `/health` にソースステータス追加

### Phase 3: フロントエンド (優先度: 中)
10. [ ] `frontend/src/types/api.ts` - 型定義更新
11. [ ] `frontend/src/components/SourceSelector.tsx` - 新規作成
12. [ ] `frontend/src/hooks/useLandings.ts` - source フィルタ対応
13. [ ] `frontend/src/components/FilterBar.tsx` - ソースセレクタ統合
14. [ ] `frontend/src/components/LandingTable.tsx` - source_name 表示
15. [ ] `frontend/src/views/Dashboard.tsx` - 統合
16. [ ] `frontend/src/views/Detail.tsx` - source_name 表示

### Phase 4: テスト・ドキュメント (優先度: 中)
17. [ ] 単体テスト: 設定パース、複数ソース同時起動
18. [ ] 統合テスト: ソースごとの着陸検出・分離
19. [ ] `README.md` 更新: 環境変数例・設定方法
20. [ ] `.env.example` 更新

---

## 8. 移行戦略と後方互換性

### 8.1 既存ユーザーへの影響
- **設定変更なしで動作継続**: `DLT_TACVIEW_HOST`/`PORT` のみ設定されている場合、内部的に `id="default"` の単一ソースとして扱われる
- **DB マイグレーション**: 既存レコードは `source_id='default'` で自動埋め込み
- **API レスポンス**: `source_id` フィールドが追加されるが、既存クライアントは無視して動作

### 8.2 段階的移行
1. まずバックエンドのみデプロイ (フロントエンドは現状維持)
2. 動作確認後、フロントエンドにソースセレクタ追加
3. 必要に応じて既存データの `source_id` を実環境に合わせて更新

---

## 9. 考慮事項・未解決項目

### 9.1 パフォーマンス
- 複数ソース同時接続時のメモリ・CPU 使用量増加
- 各ソース独立の ingestor + DB セッション = 接続数増加
- 必要に応じて接続プールサイズ調整 (`pool_size` in database_url)

### 9.2 同一機体の重複検出
- 異なるソースで同一機体 (同一 ACMI ID) が見えた場合の扱い
- 現状: `source_id` + `flight_id` + `acmi_id` で一意制約 → 別フライトとして扱われる (問題なし)

### 9.3 設定の動的変更
- 実行中のソース追加・削除・無効化は現状非対応 (再起動必要)
- 将来的には API で動的管理可能に拡張検討

### 9.4 フロントエンドのソース永続化
- ユーザーが選択したソースフィルタを localStorage で保存・復元
- 初期値は「すべてのソース」

---

## 10. まとめ

この設計により、DCSWebGCA と同等の「1プロセスで複数 DCS サーバ同時購読」が実現可能。既存単一サーバ運用は無変更で継続可能。実装は Phase 1〜4 の順序で進め、各 Phase ごとにテスト・動作確認を行う。

---