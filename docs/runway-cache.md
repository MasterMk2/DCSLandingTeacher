# 滑走路キャッシュの保存と引き継ぎ

Compose の API は `/data` を名前付きボリューム `runway_cache` に保存する。
キャッシュは `/data/cache/runways-<theatre>.json` にあり、API コンテナを再作成しても残る。
`docker compose down` はボリュームを保持する。`down -v` は DB とキャッシュを削除するため、
保存が必要な環境では実行しない。

## 以前の匿名ボリュームから引き継ぐ

名前付きボリュームへの変更だけでは、以前の匿名ボリュームの内容は移らない。
旧コンテナを削除する前に API を停止し、保存先を確認する。

```powershell
docker compose stop api
docker inspect (docker compose ps -a -q api) --format '{{json .Mounts}}'
```

`Destination` が `/data` の mount の `Name` を控え、バックアップを取る。
以下のプレースホルダーは確認したボリューム名とホストの保存先へ置き換える。

```powershell
docker run --rm --mount type=volume,source=<旧ボリューム名>,target=/source,readonly --mount type=bind,source=<バックアップ先の絶対パス>,target=/backup alpine tar -czf /backup/runway-cache.tar.gz -C /source cache
```

更新後の Compose で API コンテナを作成し、同じ inspect コマンドで新しい `/data` の
ボリューム名を確認する。API の起動前に内容をコピーする。

```powershell
docker compose create api
docker run --rm --mount type=volume,source=<旧ボリューム名>,target=/source,readonly --mount type=volume,source=<新ボリューム名>,target=/target alpine sh -c 'cp -a /source/cache/. /target/cache/'
docker compose up -d api
```

キャッシュを引き継がず DCSServerBot から再取得してもよい。同梱 exact seed のある
マップは、再取得したライブキャッシュより exact seed を優先する。引き継ぎ後は
`GET /api/v1/runways/theatres` で収録マップを確認する。
