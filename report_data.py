"""매일 아침 보고서용 데이터 정리 (Claude 예약 작업이 실행).

수집된 순위 파일을 읽어서
  - 서점별 주목할 책 표 (주간 / 일간)
  - 원인 조사 대상 (주간 최대 3권 + 일간 최대 3권)
  - [청소년] 목록
  - 수집 실패 여부
를 JSON으로 만든다. 메일 HTML은 render_email.py가 이 JSON과
Claude가 쓴 원인 분석(analysis.json)을 합쳐서 만든다.

실행: python report_data.py [--date YYYY-MM-DD] --out /tmp/report.json
"""
import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from rules import threshold

KST = timezone(timedelta(hours=9))
DATA = Path(__file__).parent / "data"
SNAP_DIR = DATA / "snapshots"
PAGE_URL = "https://editor-taek.github.io/bookrank.taek/"
STORES = {"kyobo": "교보문고", "yes24": "예스24", "aladin": "알라딘"}
LISTS = {"weekly": "주간", "daily": "일간"}
WEEKDAYS = "월화수목금토일"
PICKS_PER_LIST = 3
BOARD_LIMIT = 15   # 메일 표에 보여 줄 최대 권수 (나머지는 웹페이지)
RECENT_DAYS = 3


def load_snap(d):
    p = SNAP_DIR / f"{d.isoformat()}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def norm_title(t):
    t = re.split(r"\s+-\s+", t or "")[0]          # 알라딘 부제 ' - ...' 제거
    t = re.sub(r"[\(\[\{（].*?[\)\]\}）]", "", t)   # 괄호 속 판 정보 제거
    return re.sub(r"[^0-9A-Za-z가-힣]", "", t).lower()


def book_key(it):
    return it.get("isbn") or "t:" + norm_title(it.get("title"))


def lst(snap, store, kind):
    if not snap:
        return None
    return snap.get("stores", {}).get(store, {}).get(kind)


def window_dates(d):
    """보고서가 다루는 수집일. 월요일은 토·일·월(주말 종합)."""
    if d.weekday() == 0:
        return [d - timedelta(days=2), d - timedelta(days=1), d]
    return [d]


def collect_hits(d, kind):
    """기간 안에서 주목할 책이 된 기록. {store: {item_id: info}}"""
    days = window_dates(d)
    monday = d.weekday() == 0
    fri = load_snap(d - timedelta(days=3)) if monday else None
    cur = load_snap(d)
    out = {}
    for store in STORES:
        res = {}
        for day in days:
            l = lst(load_snap(day), store, kind)
            if not l or l.get("status") != "ok":
                continue
            for it in l["items"]:
                if not it.get("notable"):
                    continue
                prev = res.get(it["id"])
                if not prev or (it.get("multiplier") or 0) > (prev["multiplier"] or 0):
                    res[it["id"]] = dict(it, hit_date=day.isoformat())
        # 월요일: 금요일 → 월요일 순위 변화(우리 수집분 기준)도 반영
        if monday:
            cl, fl = lst(cur, store, kind), lst(fri, store, kind)
            if cl and cl.get("status") == "ok" and fl and fl.get("status") == "ok" and cl.get("updated", True):
                fri_rank = {x["id"]: x["rank"] for x in fl["items"]}
                for it in cl["items"]:
                    fr = fri_rank.get(it["id"])
                    rise = (fr if fr else 101) - it["rank"]
                    th = threshold(it["rank"])
                    if th and rise >= th:
                        mult = round(rise / th, 2)
                        prev = res.get(it["id"])
                        if not prev or mult > (prev["multiplier"] or 0):
                            res[it["id"]] = dict(it, multiplier=mult, rise=rise, hit_date=d.isoformat(),
                                                 change_label=("NEW" if not fr else f"▲{rise}") + "(금→월)")
            # 현재(월요일) 순위·금→월 변동 표시용
            if cl and cl.get("status") == "ok":
                cur_by_id = {x["id"]: x for x in cl["items"]}
                fri_rank = {x["id"]: x["rank"] for x in (fl or {}).get("items", [])} if fl else {}
                for iid, h in res.items():
                    c = cur_by_id.get(iid)
                    h["now_rank"] = c["rank"] if c else None
                    fr = fri_rank.get(iid)
                    h["fri_rank"] = fr
        out[store] = res
    return out


def group_hits(hits, include_teen=False):
    """서점별 주목 기록을 '같은 책' 단위로 묶고 우선순위대로 정렬."""
    groups, by_title = {}, {}
    for store, res in hits.items():
        for it in res.values():
            if it.get("teen") and not include_teen:
                continue  # 청소년 책은 원인 보고서 없음
            k = book_key(it)
            nt = norm_title(it["title"])
            if k not in groups and not it.get("isbn") and nt in by_title:
                k = by_title[nt]  # ISBN을 모르는 책은 제목으로 같은 책 찾기
            g = groups.setdefault(k, {"key": k, "title": it["title"], "author": it["author"],
                                      "publisher": it["publisher"], "isbn": it.get("isbn"),
                                      "link": it["link"], "teen": bool(it.get("teen")), "hits": []})
            by_title.setdefault(nt, k)
            g["hits"].append({"store": store, "store_name": STORES[store], "rank": it.get("now_rank") or it["rank"],
                              "change_label": it["change_label"], "multiplier": it.get("multiplier"),
                              "fri_rank": it.get("fri_rank"), "date": it.get("hit_date"), "link": it["link"]})
    ranked = []
    for g in groups.values():
        g["stores_count"] = len({h["store"] for h in g["hits"]})
        g["max_multiplier"] = max((h["multiplier"] or 0) for h in g["hits"])
        g["best_rank"] = min(h["rank"] for h in g["hits"])
        ranked.append(g)
    ranked.sort(key=lambda g: (-g["stores_count"], -g["max_multiplier"], g["best_rank"]))
    return ranked


def select_picks(d, kind, recent):
    """원인 조사 대상 고르기. recent: {book_key: multiplier} 최근 3일 보고분."""
    ranked = group_hits(collect_hits(d, kind))
    picks, skipped = [], []
    for g in ranked:
        if len(picks) >= PICKS_PER_LIST:
            break
        if g["key"] in recent and g["max_multiplier"] <= recent[g["key"]]:
            skipped.append({"title": g["title"], "reason": "최근 3일 안에 보고함"})
            continue
        picks.append(g)
    return picks, skipped, len(ranked)


_memo = {}


def picks_for(d):
    """d일 보고서의 원인 조사 대상 (주간·일간). 최근 보고 이력은 과거 날짜를 같은 규칙으로 재계산해 얻는다."""
    if d in _memo:
        return _memo[d]
    if d.weekday() >= 5 or load_snap(d) is None or d < datetime.now(KST).date() - timedelta(days=20):
        _memo[d] = {"weekly": [], "daily": []}
        return _memo[d]
    recent = recent_reports(d)
    res = {kind: select_picks(d, kind, recent)[0] for kind in LISTS}
    _memo[d] = res
    return res


def recent_reports(d):
    recent = {}
    for back in range(1, RECENT_DAYS + 1):
        prev = picks_for(d - timedelta(days=back))
        for kind in LISTS:
            for g in prev[kind]:
                recent[g["key"]] = max(recent.get(g["key"], 0), g["max_multiplier"])
    return recent


def current_index(snap, kind):
    """같은 책의 다른 서점 현재 순위를 찾기 위한 색인."""
    idx = {}
    for store in STORES:
        l = lst(snap, store, kind)
        by = {}
        if l and l.get("status") == "ok":
            for it in l["items"]:
                by.setdefault(book_key(it), it)
                by.setdefault("t:" + norm_title(it["title"]), it)
        idx[store] = by
    return idx


def cell(it, notable, fri_rank=None, change_label=None):
    return {"rank": it["rank"], "change_label": change_label or it.get("change_label", ""),
            "change_type": it.get("change_type"), "notable": notable, "fri_rank": fri_rank}


def build(d):
    snap = load_snap(d)
    out = {"date": d.isoformat(), "weekday": WEEKDAYS[d.weekday()], "is_weekend": d.weekday() >= 5,
           "monday_mode": d.weekday() == 0, "window": [x.isoformat() for x in window_dates(d)],
           "page_url": PAGE_URL, "snapshot_found": snap is not None,
           "collected_at": (snap or {}).get("collected_at"), "collection": {}, "sections": {}, "teen": []}
    for store, sname in STORES.items():
        for kind, kname in LISTS.items():
            l = lst(snap, store, kind) or {}
            out["collection"][f"{store}.{kind}"] = {
                "store": sname, "list": kname, "status": l.get("status", "missing"),
                "error": l.get("error"), "note": l.get("note"), "collected_at": l.get("collected_at"),
                "updated": l.get("updated", True)}
    if out["is_weekend"] or not snap:
        return out

    recent = recent_reports(d)
    picked_keys = {}
    for kind in LISTS:
        ranked = group_hits(collect_hits(d, kind))
        idx = current_index(snap, kind)
        board = []
        for g in ranked:
            cells = {}
            for h in g["hits"]:
                c = cell(h, True, h.get("fri_rank"), h["change_label"])
                c["rank"] = h["rank"]
                cells[h["store"]] = c
            for store in STORES:
                if store in cells:
                    continue
                it = idx[store].get(g["key"]) or idx[store].get("t:" + norm_title(g["title"]))
                cells[store] = cell(it, False) if it else None
            board.append({"key": g["key"], "title": g["title"], "author": g["author"],
                          "publisher": g["publisher"], "link": g["link"], "cells": cells,
                          "stores_count": g["stores_count"]})
        picks, skipped, n = select_picks(d, kind, recent)
        for g in picks:
            g["also_in"] = [k for k, keys in picked_keys.items() if g["key"] in keys]
        picked_keys[kind] = {g["key"] for g in picks}
        out["sections"][kind] = {"list_name": LISTS[kind], "board": board[:BOARD_LIMIT],
                                 "board_total": len(board), "picks": picks,
                                 "skipped_recent": skipped, "candidate_count": n,
                                 "notable_counts": {s: sum(1 for b in board if b["cells"].get(s) and b["cells"][s]["notable"])
                                                    for s in STORES}}

    # 청소년: 책 단위로 묶어 서점·주간/일간 순위를 한 줄에
    teen = {}
    for store, sname in STORES.items():
        for kind, kname in LISTS.items():
            l = lst(snap, store, kind)
            if l and l.get("status") == "ok":
                for it in l["items"]:
                    if it.get("teen"):
                        k = book_key(it)
                        t = teen.setdefault(k, {"title": it["title"], "author": it["author"],
                                                "publisher": it["publisher"], "link": it["link"], "cells": {}})
                        t["cells"].setdefault(store, {})[kind] = {"rank": it["rank"], "change_label": it["change_label"],
                                                                  "change_type": it.get("change_type")}
    out["teen"] = sorted(teen.values(), key=lambda t: min(c["rank"] for s in t["cells"].values() for c in s.values()))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--out", default="/tmp/report.json")
    a = ap.parse_args()
    d = date.fromisoformat(a.date) if a.date else datetime.now(KST).date()
    r = build(d)
    r["subject"] = f"[베스트셀러] {r['date']} ({r['weekday']})"
    Path(a.out).write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    # 화면에는 원인 조사에 필요한 것만
    slim = {k: r[k] for k in ("date", "weekday", "is_weekend", "monday_mode", "snapshot_found",
                               "collected_at", "subject")}
    slim["out"] = a.out
    slim["collection_failed"] = [f"{c['store']} {c['list']}" for c in r["collection"].values() if c["status"] != "ok"]
    slim["picks"] = {k: [{"key": g["key"], "title": g["title"], "author": g["author"], "publisher": g["publisher"],
                          "hits": [f"{h['store_name']} {h['rank']}위 {h['change_label']}" for h in g["hits"]],
                          "also_in": g["also_in"]} for g in v["picks"]]
                     for k, v in r["sections"].items()}
    slim["teen_count"] = len(r["teen"])
    print(json.dumps(slim, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
