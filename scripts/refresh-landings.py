#!/usr/bin/env python3
"""保存済みの着陸を今の版に揃える（採点や検出が変わるデプロイの後に一度流す）。

- 空母の着陸は ``POST /landings/{id}/rebuild`` で DB の生の航跡から作り直す。
  取り込み窓を広げる前（grading_version 7 未満）の空母の記録は接地前 60 秒しか
  持っておらず、再採点ではキスオフも甲板と一緒に動く座標系も戻らないため。
  作り直せなかった行（409: 生の航跡が無い / 航空機ではない / その時刻に着陸が
  無い）は、行をそのまま残して再採点だけする。
- それ以外の着陸は ``POST /landings/{id}/regrade`` で再採点する。

既定は下見で、何も書かない。``--apply`` で書き込む。書き込む前に、作り直す
空母の行は詳細（``GET /landings/{id}``）を、それ以外はグレードと点数を
``--snapshot`` の JSONL に残す。

標準ライブラリだけで動くので、サーバ上の python3 に標準入力で流して使える::

    python scripts/refresh-landings.py --base http://127.0.0.1:8000/api/v1
    python scripts/refresh-landings.py --base http://127.0.0.1:8000/api/v1 --apply

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

REBUILD_PATH = "/api/v1/landings/{landing_id}/rebuild"


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code


def request(base: str, method: str, path: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    token = os.environ.get("DLT_AUTH_TOKEN")
    if token:
        req.add_header("X-Auth-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        text = error.read().decode("utf-8", "replace")
        try:
            envelope = json.loads(text)
        except ValueError:
            envelope = {}
        code = envelope.get("error") or str(envelope.get("detail") or "HTTP_ERROR")
        raise ApiError(error.code, code, envelope.get("message") or text[:200]) from None


def list_landings(base: str) -> list[dict]:
    """Every stored landing, fetched up front: a rebuild can move a row's
    touchdown time, and paging while rows move would skip or repeat some."""
    items: list[dict] = []
    while True:
        page = request(base, "GET", f"/landings?limit=200&offset={len(items)}")
        items.extend(page["items"])
        if not page["items"] or len(items) >= page["total"]:
            return items


def has_rebuild(base: str) -> bool:
    root = base.split("/api/", 1)[0]
    try:
        spec = request(root, "GET", "/openapi.json")
    except (ApiError, urllib.error.URLError):
        return False
    return REBUILD_PATH in spec.get("paths", {})


def approach_s(detail: dict) -> float | None:
    """Seconds of approach the row holds before its touchdown."""
    track = detail.get("approach_track") or {}
    samples = track.get("samples") or []
    touchdown = track.get("touchdown_time")
    if not samples or touchdown is None:
        return None
    return touchdown - samples[0]["time"]


def fmt(value, spec: str = ".0f") -> str:
    return "-" if value is None else format(value, spec)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="http://127.0.0.1:8000/api/v1")
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--snapshot", default=None, help="JSONL of the rows before (with --apply)")
    parser.add_argument("--pause", type=float, default=0.05, help="seconds between writes")
    args = parser.parse_args()
    base = args.base.rstrip("/")
    try:
        sys.stdout.reconfigure(errors="backslashreplace")
    except AttributeError:
        pass

    try:
        rows = list_landings(base)
    except (ApiError, urllib.error.URLError) as error:
        print(f"cannot list landings at {base}: {error}")
        return 2
    carriers = [row for row in rows if row.get("kind") == "carrier"]
    others = [row for row in rows if row.get("kind") != "carrier"]
    rebuild_ok = has_rebuild(base)
    print(f"{len(rows)} landings: {len(carriers)} carrier (rebuild), {len(others)} other (regrade)")
    print(f"rebuild endpoint: {'present' if rebuild_ok else 'MISSING'}")

    details: dict[int, dict] = {}
    for row in carriers:
        detail = request(base, "GET", f"/landings/{row['id']}")
        details[row["id"]] = detail
        geometry = (detail.get("approach_track") or {}).get("geometry") or {}
        print(
            f"  #{row['id']} {row.get('airframe')} at {row.get('venue_name')}: "
            f"grade={row.get('grade')} approach={fmt(approach_s(detail))}s "
            f"frame={geometry.get('frame')} v{detail.get('grading_version')}"
        )

    if not args.apply:
        print("dry run: nothing written (add --apply to write)")
        return 0
    if carriers and not rebuild_ok:
        print("this server has no rebuild endpoint yet: deploy first; nothing written")
        return 2

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot = args.snapshot or f"landing-refresh-snapshot-{stamp}.jsonl"
    with open(snapshot, "w", encoding="utf-8") as out:
        for landing_id, detail in details.items():
            out.write(json.dumps({"id": landing_id, "detail": detail}) + "\n")
        for row in others:
            keep = ("id", "kind", "grade", "score", "approach_pattern")
            out.write(json.dumps({key: row.get(key) for key in keep}) + "\n")
    print(f"snapshot of the rows before: {os.path.abspath(snapshot)}")

    rebuilt = 0
    refused: dict[str, list[int]] = {}
    failures: list[str] = []
    for row in carriers:
        before = details[row["id"]]
        try:
            after = request(base, "POST", f"/landings/{row['id']}/rebuild")
        except ApiError as error:
            if error.status != 409:
                failures.append(f"rebuild #{row['id']}: {error}")
                continue
            refused.setdefault(error.code, []).append(row["id"])
            others.append(row)
            print(f"  #{row['id']} left as it was ({error.code}); regrading it instead")
            continue
        rebuilt += 1
        start = after.get("approach_start_time")
        touchdown = after.get("touchdown_time")
        approach = touchdown - start if start is not None and touchdown is not None else None
        print(
            f"  #{row['id']} rebuilt: grade {before.get('grade')} -> {after.get('grade')}, "
            f"approach {fmt(approach_s(before))}s -> {fmt(approach)}s, "
            f"{after.get('kind')}/{after.get('outcome')}, "
            f"entry={(after.get('metrics') or {}).get('pattern_entry')}"
        )
        time.sleep(args.pause)

    regraded = changed_grade = changed_score = 0
    for row in others:
        try:
            after = request(base, "POST", f"/landings/{row['id']}/regrade", {})
        except ApiError as error:
            failures.append(f"regrade #{row['id']}: {error}")
            continue
        regraded += 1
        if after.get("grade") != row.get("grade"):
            changed_grade += 1
            print(
                f"  #{row['id']} {row.get('airframe')}: grade {row.get('grade')} -> "
                f"{after.get('grade')} (score {fmt(row.get('score'), '.1f')} -> "
                f"{fmt(after.get('score'), '.1f')})"
            )
        elif row.get("score") != after.get("score"):
            changed_score += 1
        time.sleep(args.pause)

    print(f"rebuilt {rebuilt} carrier landing(s)")
    for code, ids in sorted(refused.items()):
        print(f"not rebuilt ({code}): {', '.join(f'#{i}' for i in ids)}")
    print(
        f"regraded {regraded}: grade changed on {changed_grade}, "
        f"score only on {changed_score}"
    )
    for failure in failures:
        print(f"FAILED {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
