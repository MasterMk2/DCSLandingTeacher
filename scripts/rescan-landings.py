#!/usr/bin/env python3
"""DB に残っている生の航跡を今の検出器で洗い直し、記録されていない着陸を拾う。

生の航跡を着陸の前後だけに縮める（``python -m app.compact``）前に一度だけ流す。
縮めた後は、着陸の窓の外にあった航跡はもう戻らない。

- フライト（ACMI のセッション）ごとに ``POST /flights/{id}/rescan`` を呼ぶ。
  サーバが機体ごとの全航跡を読み、ライブの取り込みと同じ判定で接地を探して、
  見つけた着陸を作り直し（rebuild）と同じ切り方で検出し直す。
- 既に記録されている着陸には触らない。記録されていない着陸だけが対象。
- 既定は下見で、何も書かない。``--apply`` で見つけた着陸を採点して記録する
  （WebSocket での通知はしない。日時はセッションの開始時刻から推定する）。
- 1 フライトごとの結果を ``--report`` の JSONL に残す。途中で止まっても
  ``--from-flight`` でそこから続けられる（``--apply`` で記録した分は、2 回目は
  「既に記録済み」として数えられるだけで二重には入らない）。
- ファイルのインポート（``import:*``）のフライトは既定では飛ばす。

標準ライブラリだけで動くので、サーバ上の python3 に標準入力で流して使える::

    python scripts/rescan-landings.py --base http://127.0.0.1:8000/api/v1
    python scripts/rescan-landings.py --base http://127.0.0.1:8000/api/v1 --apply

``DLT_AUTH_TOKEN`` を設定しているサーバでは、同じ名前の環境変数に入れておくと
``X-Auth-Token`` で送る。出力は ASCII だけにしてある（ssh 越しに Windows の
端末へ返しても化けない）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

RESCAN_PATH = "/api/v1/flights/{flight_id}/rescan"
IMPORT_PREFIX = "import:"


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code


def request(base: str, method: str, path: str, timeout: float = 120.0) -> object:
    req = urllib.request.Request(base + path, method=method)
    req.add_header("Accept", "application/json")
    token = os.environ.get("DLT_AUTH_TOKEN")
    if token:
        req.add_header("X-Auth-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        text = error.read().decode("utf-8", "replace")
        try:
            envelope = json.loads(text)
        except ValueError:
            envelope = {}
        code = envelope.get("error") or str(envelope.get("detail") or "HTTP_ERROR")
        raise ApiError(error.code, code, envelope.get("message") or text[:200]) from None


def has_rescan(base: str) -> bool:
    root = base.split("/api/", 1)[0]
    try:
        spec = request(root, "GET", "/openapi.json")
    except (ApiError, urllib.error.URLError):
        return False
    return RESCAN_PATH in spec.get("paths", {})


def ascii_text(value: object) -> str:
    return str(value).encode("ascii", "backslashreplace").decode("ascii")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="http://127.0.0.1:8000/api/v1")
    parser.add_argument("--apply", action="store_true", help="store what is found (default: dry run)")
    parser.add_argument("--from-flight", type=int, default=0, help="skip flights with a lower id")
    parser.add_argument("--include-imports", action="store_true", help="also rescan import:* flights")
    parser.add_argument("--report", default=None, help="JSONL of every flight's result")
    parser.add_argument("--timeout", type=float, default=3600.0, help="seconds per flight")
    args = parser.parse_args()
    base = args.base.rstrip("/")

    if not has_rescan(base):
        print(f"{base} has no rescan endpoint: deploy the version with it first")
        return 2
    try:
        flights = request(base, "GET", "/flights")
    except (ApiError, urllib.error.URLError) as error:
        print(f"cannot list flights at {base}: {error}")
        return 2
    todo = [
        f
        for f in flights
        if f["id"] >= args.from_flight
        and (args.include_imports or not str(f.get("source_id") or "").startswith(IMPORT_PREFIX))
    ]
    print(
        f"{len(flights)} flights, rescanning {len(todo)} "
        f"({'APPLY: new landings will be stored' if args.apply else 'dry run: nothing written'})"
    )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = args.report or f"landing-rescan-{'apply' if args.apply else 'dry'}-{stamp}.jsonl"
    totals = {"samples": 0, "found": 0, "new": 0, "created": 0, "unmatched": 0}
    failures: list[str] = []
    started = time.monotonic()
    with open(report_path, "a", encoding="utf-8") as out:
        for index, flight in enumerate(todo, 1):
            path = f"/flights/{flight['id']}/rescan" + ("?apply=true" if args.apply else "")
            try:
                result = request(base, "POST", path, timeout=args.timeout)
            except (ApiError, urllib.error.URLError, TimeoutError) as error:
                failures.append(f"flight {flight['id']}: {error}")
                print(f"[{index}/{len(todo)}] flight {flight['id']}: FAILED {error}")
                continue
            out.write(json.dumps(result) + "\n")
            out.flush()
            new = [item for item in result["landings"] if item["existing_landing_id"] is None]
            created = [item for item in new if item.get("created_landing_id") is not None]
            totals["samples"] += result["samples_scanned"]
            totals["found"] += len(result["landings"])
            totals["new"] += len(new)
            totals["created"] += len(created)
            totals["unmatched"] += len(result["stored_not_redetected"])
            print(
                f"[{index}/{len(todo)}] flight {flight['id']} ({flight.get('created_at')}): "
                f"{result['aircraft_scanned']} aircraft, {result['samples_scanned']:,} samples, "
                f"{len(result['landings'])} landings, {len(new)} not stored before"
                + (f", {len(created)} stored now" if args.apply else "")
                + f" [{result['elapsed_s']:.0f}s]"
            )
            for item in new:
                print(
                    "    + "
                    + ascii_text(
                        f"obj {item['acmi_id']} {item.get('airframe')} "
                        f"({item.get('pilot')}): {item['kind']}/{item['outcome']} "
                        f"t={item['touchdown_time']:.1f}"
                        + (f" on {item['carrier_name']}" if item.get("carrier_name") else "")
                        + (
                            f" -> #{item['created_landing_id']}"
                            if item.get("created_landing_id")
                            else ""
                        )
                    )
                )
            if result["stored_not_redetected"]:
                ids = ", ".join(f"#{i}" for i in result["stored_not_redetected"])
                print(f"    stored but not re-detected (left as they are): {ids}")

    print(
        f"done in {time.monotonic() - started:.0f}s: {totals['samples']:,} samples, "
        f"{totals['found']} landings found, {totals['new']} not stored before"
        + (f", {totals['created']} stored now" if args.apply else "")
        + f"; {totals['unmatched']} stored landings not re-detected"
    )
    print(f"report: {os.path.abspath(report_path)}")
    for failure in failures:
        print(f"FAILED {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
