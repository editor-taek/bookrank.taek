"""매일 아침 보고서용 데이터 정리 (Claude 예약 작업이 실행).

수집된 순위 파일을 읽어서
  - 서점별 주목할 책 표 (주간 종합 / 일간)
  - 원인 조사 대상 (주간 최대 3권 + 일간 최대 3권)
  - [청소년] 목록
  - 수집 실패 여부
를 JSON으로 출력하고, --html 을 주면 메일 본문 뼈대(표가 채워진 HTML)도 만든다.
원인 분석·요약 문장은 Claude가 웹 검색 후 뼈대의 자리표시({{...}})에 채운다.

실행: python report_data.py [--date YYYY-MM-DD] [--html report.html]
"""
import argparse
import html
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
LISTS = {"weekly": "주간 종합", "daily": "일간"}
WEEKDAYS = "월화수목금토일"
PICKS_PER_LIST = 3
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


def select_picks(d, kind, recent):
    """원인 조사 대상 고르기. recent: {book_key: multiplier} 최근 3일 보고분."""
    hits = collect_hits(d, kind)
    groups, by_title = {}, {}
    for store, res in hits.items():
        for it in res.values():
            if it.get("teen"):
                continue  # 청소년 책은 원인 보고서 없음
            k = book_key(it)
            nt = norm_title(it["title"])
            if k not in groups and not it.get("isbn") and nt in by_title:
                k = by_title[nt]  # ISBN을 모르는 책은 제목으로 같은 책 찾기
            g = groups.setdefault(k, {"key": k, "title": it["title"], "author": it["author"],
                                      "publisher": it["publisher"], "isbn": it.get("isbn"), "hits": []})
            by_title.setdefault(nt, k)
            g["hits"].append({"store": store, "store_name": STORES[store], "rank": it.get("now_rank") or it["rank"],
                              "change_label": it["change_label"], "multiplier": it.get("multiplier"),
                              "date": it.get("hit_date"), "link": it["link"]})
    ranked = []
    for g in groups.values():
        g["stores_count"] = len({h["store"] for h in g["hits"]})
        g["max_multiplier"] = max((h["multiplier"] or 0) for h in g["hits"])
        g["best_rank"] = min(h["rank"] for h in g["hits"])
        ranked.append(g)
    ranked.sort(key=lambda g: (-g["stores_count"], -g["max_multiplier"], g["best_rank"]))
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
    recent = {}
    for back in range(1, RECENT_DAYS + 1):
        prev = picks_for(d - timedelta(days=back))
        for kind in LISTS:
            for g in prev[kind]:
                recent[g["key"]] = max(recent.get(g["key"], 0), g["max_multiplier"])
    res = {}
    for kind in LISTS:
        res[kind] = select_picks(d, kind, recent)[0]
    _memo[d] = res
    return res


def build(d):
    snap = load_snap(d)
    out = {"date": d.isoformat(), "weekday": WEEKDAYS[d.weekday()], "is_weekend": d.weekday() >= 5,
           "monday_mode": d.weekday() == 0, "window": [x.isoformat() for x in window_dates(d)],
           "page_url": PAGE_URL, "snapshot_found": snap is not None, "collection": {}, "sections": {}, "teen": []}
    for store, sname in STORES.items():
        for kind, kname in LISTS.items():
            l = lst(snap, store, kind) or {}
            out["collection"][f"{store}.{kind}"] = {
                "store": sname, "list": kname, "status": l.get("status", "missing"),
                "error": l.get("error"), "note": l.get("note"), "collected_at": l.get("collected_at"),
                "updated": l.get("updated", True)}
    if out["is_weekend"] or not snap:
        return out

    # 최근 3일 보고 이력
    recent = {}
    for back in range(1, RECENT_DAYS + 1):
        prev = picks_for(d - timedelta(days=back))
        for kind in LISTS:
            for g in prev[kind]:
                recent[g["key"]] = max(recent.get(g["key"], 0), g["max_multiplier"])

    picked_keys = {}
    for kind in LISTS:
        hits = collect_hits(d, kind)
        tables = {}
        for store in STORES:
            rows = list(hits[store].values())
            # 청소년 책은 상승 여부와 관계없이 표에 [청소년]으로 올림
            l = lst(snap, store, kind)
            if l and l.get("status") == "ok":
                ids = {r["id"] for r in rows}
                rows += [it for it in l["items"] if it.get("teen") and it["id"] not in ids]
            rows.sort(key=lambda r: (r.get("now_rank") or r["rank"]))
            tables[store] = [{"rank": r.get("now_rank") or r["rank"], "change_label": r["change_label"],
                              "fri_rank": r.get("fri_rank"), "hit_date": r.get("hit_date"),
                              "title": r["title"], "author": r["author"], "publisher": r["publisher"],
                              "teen": bool(r.get("teen")), "notable": bool(r.get("notable") or r.get("hit_date")),
                              "link": r["link"]} for r in rows]
        picks, skipped, n = select_picks(d, kind, recent)
        for g in picks:
            g["also_in"] = [k for k, keys in picked_keys.items() if g["key"] in keys]
        picked_keys[kind] = {g["key"] for g in picks}
        out["sections"][kind] = {"list_name": LISTS[kind], "tables": tables, "picks": picks,
                                 "skipped_recent": skipped, "candidate_count": n}

    for store, sname in STORES.items():
        for kind, kname in LISTS.items():
            l = lst(snap, store, kind)
            if l and l.get("status") == "ok":
                for it in l["items"]:
                    if it.get("teen"):
                        out["teen"].append({"store": sname, "list": kname, "rank": it["rank"],
                                            "change_label": it["change_label"], "title": it["title"],
                                            "author": it["author"], "publisher": it["publisher"], "link": it["link"]})
    return out


# ------------------------------------------------------------------ 메일 뼈대 HTML
CSS = ("body{font-family:'Apple SD Gothic Neo','Malgun Gothic',sans-serif;color:#222;line-height:1.55;max-width:760px}"
       "h2{font-size:18px;border-bottom:2px solid #222;padding-bottom:4px;margin-top:28px}"
       "h3{font-size:15px;margin:18px 0 6px}"
       "table{border-collapse:collapse;width:100%;font-size:13px;margin-bottom:10px}"
       "th,td{border:1px solid #ddd;padding:4px 6px;text-align:left;vertical-align:top}"
       "th{background:#f4f4f4}.teen{background:#e8f3ff;color:#0b5cad;font-weight:bold;padding:0 4px;border-radius:3px}"
       ".up{color:#d0021b;font-weight:bold}.note{color:#888;font-size:12px}.fail{color:#d0021b;font-weight:bold}")


def e(s):
    return html.escape(str(s or ""))


def table_html(rows, monday):
    if not rows:
        return '<p class="note">해당 없음</p>'
    head = "<tr><th>순위</th><th>변동</th>" + ("<th>금요일</th>" if monday else "") + "<th>제목</th><th>저자 · 출판사</th></tr>"
    body = ""
    for r in rows:
        tag = '<span class="teen">[청소년]</span> ' if r["teen"] else ""
        cls = ' class="up"' if r["change_label"].startswith(("▲", "NEW")) else ""
        fri = f"<td>{e(r['fri_rank'] or '100위 밖')}</td>" if monday else ""
        body += (f"<tr><td>{r['rank']}</td><td{cls}>{e(r['change_label'])}</td>{fri}"
                 f"<td>{tag}<a href=\"{e(r['link'])}\">{e(r['title'])}</a></td>"
                 f"<td>{e(r['author'])} · {e(r['publisher'])}</td></tr>")
    return f"<table>{head}{body}</table>"


def render_html(r):
    title = f"[베스트셀러] {r['date']} ({r['weekday']})"
    parts = [f"<html><head><meta charset='utf-8'><style>{CSS}</style></head><body>", f"<h1 style='font-size:20px'>{e(title)}</h1>"]
    fails = [c for c in r["collection"].values() if c["status"] != "ok"]
    if not r["snapshot_found"]:
        parts.append('<p class="fail">⚠ 오늘 순위 수집 실패 — 수집 파일이 없습니다. 깃허브 액션 실행 기록을 확인해 주세요.</p>')
    elif fails:
        parts.append('<p class="fail">⚠ 수집 실패: ' + ", ".join(f"{c['store']} {c['list']}" for c in fails) + "</p>")
    if r["monday_mode"]:
        parts.append('<p class="note">월요일 보고서: 금요일 → 월요일 주말 변동을 종합했습니다 (토·일·월 수집분).</p>')
    parts.append("<h2>오늘의 한눈 요약</h2>{{SUMMARY}}")
    for kind, sec in r["sections"].items():
        parts.append(f"<h2>[{sec['list_name']}] 주목할 책</h2>")
        for store, sname in STORES.items():
            c = r["collection"][f"{store}.{kind}"]
            note = f' <span class="note">({e(c["note"])})</span>' if c.get("note") else ""
            if c["status"] != "ok":
                parts.append(f"<h3>{sname}</h3><p class='fail'>수집 실패</p>")
                continue
            parts.append(f"<h3>{sname}{note}</h3>" + table_html(sec["tables"][store], r["monday_mode"]))
        parts.append(f"<h3>원인 분석 (최대 {PICKS_PER_LIST}권)</h3>{{{{{kind.upper()}_ANALYSIS}}}}")
    parts.append("<h2>[청소년] 종합 100위 안 청소년 책</h2>")
    if r["teen"]:
        parts.append("<table><tr><th>서점</th><th>구분</th><th>순위</th><th>변동</th><th>제목</th><th>저자 · 출판사</th></tr>" + "".join(
            f"<tr><td>{e(t['store'])}</td><td>{e(t['list'])}</td><td>{t['rank']}</td><td>{e(t['change_label'])}</td>"
            f"<td><a href=\"{e(t['link'])}\">{e(t['title'])}</a></td><td>{e(t['author'])} · {e(t['publisher'])}</td></tr>"
            for t in r["teen"]) + "</table>")
    else:
        parts.append('<p class="note">오늘은 종합 100위 안에 청소년 책이 없습니다.</p>')
    parts.append(f"<h2>전체 순위</h2><p><a href=\"{PAGE_URL}\">{PAGE_URL}</a></p>")
    parts.append('<p class="note">상승폭은 서점이 표시한 순위 변동 기준(알라딘은 표시가 없어 직전 수집분과 비교). '
                 'NEW는 100위 밖에서 진입한 것으로 계산.</p></body></html>')
    return title, "\n".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--html")
    a = ap.parse_args()
    d = date.fromisoformat(a.date) if a.date else datetime.now(KST).date()
    r = build(d)
    if a.html:
        subject, body = render_html(r)
        r["subject"] = subject
        Path(a.html).write_text(body, encoding="utf-8")
        r["html_path"] = a.html
    # 출력이 너무 길지 않게 표는 요약만
    slim = dict(r)
    slim["sections"] = {k: {"list_name": v["list_name"], "picks": v["picks"], "skipped_recent": v["skipped_recent"],
                            "candidate_count": v["candidate_count"],
                            "table_counts": {s: len(t) for s, t in v["tables"].items()}}
                        for k, v in r["sections"].items()}
    print(json.dumps(slim, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
