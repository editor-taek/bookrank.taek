"""서점 3사(교보문고·예스24·알라딘) 주간 종합 / 일간 베스트셀러 1~100위 수집기.

깃허브 액션에서 하루 1번 실행된다. 결과:
  data/latest.json             오늘 수집 결과 (웹페이지·보고서가 읽음)
  data/latest.csv              같은 내용을 표 형태로
  data/snapshots/YYYY-MM-DD.json  날짜별 보관본 (최근 45일)
  data/genre_cache.json        예스24·알라딘 책의 분야(청소년 여부)·ISBN 캐시

실행: python scraper.py            (오늘 것 수집. 이미 성공한 목록은 건너뜀)
      python scraper.py --force    (오늘 것 전부 다시 수집)
"""
import argparse
import csv
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from rules import apply_rules, change_label

KST = timezone(timedelta(hours=9))
DATA = Path(__file__).parent / "data"
SNAP_DIR = DATA / "snapshots"
GENRE_CACHE = DATA / "genre_cache.json"
KEEP_DAYS = 45
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
LIST_PAUSE = 2.0     # 목록 페이지 사이 쉬는 시간(초)
DETAIL_PAUSE = 1.5   # 상세페이지 사이 쉬는 시간(초)
MAX_DETAIL_PER_RUN = int(os.environ.get("MAX_DETAIL", "300"))  # 한 번에 확인할 상세페이지 최대 수 (서점 부담 제한)

STORES = {"kyobo": "교보문고", "yes24": "예스24", "aladin": "알라딘"}
LISTS = {"weekly": "주간 종합", "daily": "일간"}

SOURCE_URLS = {
    "kyobo": {"weekly": "https://store.kyobobook.co.kr/bestseller/total/weekly",
              "daily": "https://store.kyobobook.co.kr/bestseller/online/daily?page=1"},
    "yes24": {"weekly": "https://www.yes24.com/product/category/bestseller?categoryNumber=001",
              "daily": "https://www.yes24.com/product/category/daybestseller?categoryNumber=001"},
    "aladin": {"weekly": "https://www.aladin.co.kr/shop/common/wbest.aspx?BranchType=1&start=we",
               "daily": "https://www.aladin.co.kr/shop/common/wbest.aspx?BranchType=1&BestType=DailyBest"},
}

NON_BOOK_WORDS = ("음반", "DVD", "블루레이", "Blu-ray", "LP", "GIFT", "굿즈", "문구", "기프트", "티켓", "음악")


def log(*a):
    print(*a, flush=True)


def norm_space(s):
    return re.sub(r"\s+", " ", s or "").strip()


def short_authors(names):
    names = [n for n in (norm_space(x) for x in names) if n]
    if not names:
        return ""
    return names[0] if len(names) == 1 else names[0] + " 외"


# ---------------------------------------------------------------- 교보문고
KYOBO_API = {
    "weekly": "/api/gw/best/best-seller/total?page={p}&per=50&period=002&bsslBksClstCode=A",
    "daily": "/api/gw/best/best-seller/online?page={p}&per=50&period=001&dsplDvsnCode=000&dsplTrgtDvsnCode=001",
}


def scrape_kyobo(page, kind):
    page.goto(SOURCE_URLS["kyobo"][kind], wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)
    rows, period = [], None
    for p in (1, 2):
        url = KYOBO_API[kind].format(p=p)
        text = page.evaluate("async (u) => { const r = await fetch(u); return await r.text(); }", url)
        data = json.loads(text)["data"]
        period = data.get("ymw") or period
        rows.extend(data.get("bestSeller") or [])
        time.sleep(LIST_PAUSE)
    items = []
    for r in rows:
        rank = r.get("prstRnkn")
        if not rank or rank > 100:
            continue
        isbn = str(r.get("cmdtCode") or "")
        # 책이 아닌 상품(음반·굿즈 등)은 ISBN(978/979)이 없다
        if not re.match(r"^97[89]\d{10}$", isbn):
            continue
        prev = r.get("frmrRnkn") or 0
        if prev == 0:
            ctype, change, prev_rank = "new", None, None
        else:
            change = prev - rank
            ctype = "up" if change > 0 else "down" if change < 0 else "same"
            prev_rank = prev
        clst = r.get("saleCmdtClstName") or ""
        items.append({
            "rank": rank,
            "title": norm_space(r.get("cmdtName")),
            "author": norm_space(r.get("chrcName")),
            "publisher": norm_space(r.get("pbcmName")),
            "id": r.get("saleCmdtid"),
            "isbn": isbn,
            "link": f"https://product.kyobobook.co.kr/detail/{r.get('saleCmdtid')}",
            "change_type": ctype, "change": change, "prev_rank": prev_rank,
            "change_source": "store",
            "genre": clst,
            "teen": clst == "청소년",
        })
    return {"period": period, "items": items}


# ---------------------------------------------------------------- 예스24
YES24_ROLE = re.compile(r"(편저|편역|번역|저자|역자|감수|지은이|옮긴이|엮은이|글그림|그림|사진|기획|원작)$")


def yes24_author(span):
    if not span:
        return ""
    raw = span.get_text(" ", strip=True)
    raw = re.split(r"정보\s*더\s*보기", raw)[0]
    first = raw.split("/")[0]
    first = re.sub(r"\s*외\s*\d+\s*명", ",외", first)
    parts = [p.strip() for p in first.split(",") if p.strip()]
    names = []
    for p in parts:
        p = re.sub(r"\s*\([^)]*\)", "", p).strip()
        p = YES24_ROLE.sub("", p).strip()
        p = re.sub(r"\s+(저|역|글|편|감)$", "", p).strip()
        if p and p not in ("외", "저", "역", "글", "편"):
            names.append(p)
    more = "외" in parts or len(names) > 1
    if not names:
        return norm_space(raw)
    return names[0] + (" 외" if more else "")


def scrape_yes24(page, kind):
    url = SOURCE_URLS["yes24"][kind] + "&pageNumber=1&pageSize=120"
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2500)
    soup = BeautifulSoup(page.content(), "html.parser")
    items = []
    for unit in soup.select("div.itemUnit"):
        rank_el = unit.select_one("em.ico.rank")
        name_a = unit.select_one("a.gd_name")
        if not rank_el or not name_a:
            continue
        try:
            rank = int(rank_el.get_text(strip=True))
        except ValueError:
            continue
        if rank > 100:
            continue
        kind_label = norm_space(unit.select_one("span.gd_res").get_text()) if unit.select_one("span.gd_res") else ""
        if any(w in kind_label for w in NON_BOOK_WORDS):
            continue
        href = name_a.get("href", "")
        goods_id = href.rstrip("/").split("/")[-1]
        info = unit.select_one("span.rank_info")
        cls = " ".join(info.get("class", [])) if info else ""
        num_el = info.select_one("em.txt.rank") if info else None
        num = int(num_el.get_text(strip=True)) if num_el and num_el.get_text(strip=True).isdigit() else None
        if "rank_new" in cls:
            ctype, change = "new", None
        elif "rank_up" in cls and num is not None:
            ctype, change = "up", num
        elif "rank_dn" in cls and num is not None:
            ctype, change = "down", -num
        elif "rank_even" in cls:
            ctype, change = "same", 0
        else:
            ctype, change = "unknown", None
        pub = unit.select_one("span.info_pub")
        items.append({
            "rank": rank,
            "title": norm_space(name_a.get_text()),
            "author": yes24_author(unit.select_one("span.info_auth")),
            "publisher": norm_space(pub.get_text()) if pub else "",
            "id": goods_id,
            "isbn": None,
            "link": f"https://www.yes24.com/product/goods/{goods_id}",
            "change_type": ctype, "change": change,
            "prev_rank": (rank + change) if change is not None else None,
            "change_source": "store",
            "kind_label": kind_label,
        })
    return {"period": None, "items": items}


# ---------------------------------------------------------------- 알라딘
def aladin_author_pub(li_text):
    parts = [p.strip() for p in li_text.split("|")]
    authors_part = parts[0] if parts else ""
    publisher = parts[1] if len(parts) > 1 else ""
    # 첫 번째 역할(지은이 등) 묶음의 이름만
    first_group = authors_part.split(")")[0]
    first_group = re.sub(r"\([^)]*$", "", first_group)
    names = [n.strip() for n in first_group.split(",") if n.strip()]
    return short_authors(names), publisher


def scrape_aladin(page, kind):
    items = []
    for p in (1, 2):
        url = SOURCE_URLS["aladin"][kind] + f"&page={p}"
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
        soup = BeautifulSoup(page.content(), "html.parser")
        for box in soup.select("div.ss_book_box"):
            first_div = box.find("div")
            m = re.search(r"\d+", first_div.get_text() if first_div else "")
            title_a = box.select_one("a.bo3")
            if not m or not title_a:
                continue
            rank = int(m.group())
            if rank > 100:
                continue
            cat = box.select_one("span.tit_category")
            cat_text = cat.get_text(strip=True) if cat else ""
            if cat_text and "도서" not in cat_text:
                continue
            item_id = box.get("itemid") or re.search(r"ItemId=(\d+)", title_a.get("href", "")).group(1)
            author = publisher = ""
            for li in box.select("div.ss_book_list li"):
                t = norm_space(li.get_text(" ", strip=True))
                if "|" in t and ("(지은이)" in t or "(옮긴이)" in t or "(엮은이)" in t or re.search(r"\(\w+\)", t)):
                    author, publisher = aladin_author_pub(t)
                    break
            items.append({
                "rank": rank,
                "title": norm_space(title_a.get_text()),
                "author": author,
                "publisher": publisher,
                "id": str(item_id),
                "isbn": None,
                "link": f"https://www.aladin.co.kr/shop/wproduct.aspx?ItemId={item_id}",
                "change_type": "unknown", "change": None, "prev_rank": None,
                "change_source": "computed",
            })
        time.sleep(LIST_PAUSE)
    # 같은 순위 중복 제거
    seen, uniq = set(), []
    for it in sorted(items, key=lambda x: x["rank"]):
        if it["rank"] in seen:
            continue
        seen.add(it["rank"])
        uniq.append(it)
    return {"period": None, "items": uniq}


def compute_aladin_changes(items, prev_list):
    """알라딘은 순위 변동을 표시하지 않으므로 직전 수집분과 비교해 계산."""
    if not prev_list:
        for it in items:
            it["change_type"], it["change"] = "unknown", None
        return
    prev = {x["id"]: x["rank"] for x in prev_list.get("items", [])}
    for it in items:
        pr = prev.get(it["id"])
        if pr is None:
            it["change_type"], it["change"], it["prev_rank"] = "new", None, None
        else:
            ch = pr - it["rank"]
            it["change"] = ch
            it["prev_rank"] = pr
            it["change_type"] = "up" if ch > 0 else "down" if ch < 0 else "same"


# ---------------------------------------------------------------- 분야(청소년) 판별
def parse_yes24_detail(html):
    soup = BeautifulSoup(html, "html.parser")
    paths = []
    for dl in soup.select("#infoset_goodsCate dl"):
        dt = dl.find("dt")
        if dt and "카테고리" in dt.get_text():
            paths += [norm_space(li.get_text(" ", strip=True)) for li in dl.select("li")]
    m = re.search(r"ISBN13</th>\s*<td[^>]*>\s*(97[89]\d{10})", html)
    return paths, (m.group(1) if m else None)


def parse_aladin_detail(html):
    soup = BeautifulSoup(html, "html.parser")
    paths = [norm_space(li.get_text(" ", strip=True)).replace(" 접기", "")
             for li in soup.select("ul#ulCategory > li")]
    m = re.search(r"ISBN\s*:\s*(97[89]\d{10})", re.sub(r"<[^>]+>", " ", html))
    return paths, (m.group(1) if m else None)


def is_teen(paths):
    for p in paths:
        segs = [s.strip() for s in p.split(">")]
        if len(segs) > 1 and segs[1].startswith("청소년"):
            return True
    return False


def fill_genres(ctx, snapshot, cache):
    budget = MAX_DETAIL_PER_RUN
    checked = 0
    today = snapshot["date"]
    for store in ("yes24", "aladin"):
        for kind in LISTS:
            lst = snapshot["stores"][store].get(kind)
            if not lst or lst.get("status") != "ok":
                continue
            for it in lst["items"]:
                key = f"{store}:{it['id']}"
                if key not in cache:
                    if budget <= 0:
                        continue
                    budget -= 1
                    try:
                        resp = ctx.request.get(it["link"], timeout=30000)
                        html = resp.text()
                        paths, isbn = (parse_yes24_detail if store == "yes24" else parse_aladin_detail)(html)
                        if paths:
                            cache[key] = {"teen": is_teen(paths), "paths": paths[:4], "isbn": isbn,
                                          "title": it["title"], "checked": today}
                        checked += 1
                    except Exception as e:  # noqa
                        log(f"    상세페이지 실패 {key}: {e}")
                    time.sleep(DETAIL_PAUSE)
                c = cache.get(key)
                it["teen"] = c["teen"] if c else None
                if c:
                    it["isbn"] = c.get("isbn")
                    it["genre"] = (c["paths"][0].split(">")[1].strip() if c["paths"] and ">" in c["paths"][0] else "")
    log(f"  새 책 분야 확인: {checked}권")


# ---------------------------------------------------------------- 저장/불러오기
def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def previous_snapshot(today, store, kind):
    """오늘 이전 가장 최근의 정상 수집분 (날짜, 목록)."""
    if not SNAP_DIR.exists():
        return None, None
    for f in sorted(SNAP_DIR.glob("????-??-??.json"), reverse=True):
        if f.stem >= today:
            continue
        snap = load_json(f, None)
        lst = (snap or {}).get("stores", {}).get(store, {}).get(kind)
        if lst and lst.get("status") == "ok":
            return f.stem, lst
    return None, None


def write_csv(snapshot, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["날짜", "서점", "구분", "순위", "변동", "제목", "저자", "출판사", "청소년", "주목", "링크"])
        for store, sname in STORES.items():
            for kind, kname in LISTS.items():
                lst = snapshot["stores"][store].get(kind) or {}
                for it in lst.get("items", []):
                    w.writerow([snapshot["date"], sname, kname, it["rank"], change_label(it), it["title"],
                                it["author"], it["publisher"],
                                "Y" if it.get("teen") else ("?" if it.get("teen") is None else ""),
                                "Y" if it.get("notable") else "", it["link"]])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    DATA.mkdir(exist_ok=True)
    SNAP_DIR.mkdir(exist_ok=True)
    snap_path = SNAP_DIR / f"{today}.json"

    snapshot = None if args.force else load_json(snap_path, None)
    if not snapshot:
        snapshot = {"date": today, "stores": {s: {} for s in STORES}}
    todo = [(s, k) for s in STORES for k in LISTS
            if (snapshot["stores"].get(s, {}).get(k) or {}).get("status") != "ok"]
    if not todo:
        log(f"{today} 수집은 이미 끝났어요. 할 일 없음.")
        return 0
    log(f"{now:%Y-%m-%d %H:%M} 수집 시작: {', '.join(f'{STORES[s]} {LISTS[k]}' for s, k in todo)}")

    cache = load_json(GENRE_CACHE, {})
    scrapers = {"kyobo": scrape_kyobo, "yes24": scrape_yes24, "aladin": scrape_aladin}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, locale="ko-KR", viewport={"width": 1300, "height": 900})
        page = ctx.new_page()
        for store, kind in todo:
            entry = {"status": "failed", "source_url": SOURCE_URLS[store][kind],
                     "collected_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M"), "items": []}
            for attempt in (1, 2):
                try:
                    res = scrapers[store](page, kind)
                    if len(res["items"]) < 50:
                        raise RuntimeError(f"책이 {len(res['items'])}권밖에 안 읽혔어요 (페이지 구조 변경 가능성)")
                    entry.update(status="ok", period=res["period"], items=res["items"], error=None)
                    break
                except Exception as e:
                    entry["error"] = f"{type(e).__name__}: {e}"
                    log(f"  {STORES[store]} {LISTS[kind]} 실패({attempt}회): {e}")
                    traceback.print_exc()
                    time.sleep(5)
            if entry["status"] == "ok":
                prev_date, prev_list = previous_snapshot(today, store, kind)
                entry["compared_to"] = prev_date
                entry["updated"] = True
                entry["note"] = ""
                if store == "aladin":
                    compute_aladin_changes(entry["items"], prev_list)
                    entry["note"] = ("알라딘은 순위 변동을 표시하지 않아 직전 수집분"
                                     f"({prev_date})과 비교해 계산" if prev_date else
                                     "알라딘은 순위 변동을 표시하지 않음 — 비교할 이전 수집분이 없어 변동 미산출")
                if store == "kyobo" and kind == "weekly" and prev_list and prev_list.get("period") == entry["period"]:
                    entry["updated"] = False
                    entry["note"] = "교보 주간 종합은 주 1회 갱신 — 오늘은 갱신되지 않아 '변동 없음'으로 처리"
                log(f"  {STORES[store]} {LISTS[kind]}: {len(entry['items'])}권")
            snapshot["stores"].setdefault(store, {})[kind] = entry
            time.sleep(LIST_PAUSE)

        log("분야(청소년) 확인 중...")
        fill_genres(ctx, snapshot, cache)
        browser.close()

    # 주목할 책 판정
    for store in STORES:
        for kind in LISTS:
            lst = snapshot["stores"][store].get(kind)
            if not lst:
                continue
            for it in lst.get("items", []):
                apply_rules(it)
                it["change_label"] = change_label(it)
                if not lst.get("updated", True):
                    it["notable"] = False

    snapshot["collected_at"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    text = json.dumps(snapshot, ensure_ascii=False, indent=1)
    snap_path.write_text(text, encoding="utf-8")
    (DATA / "latest.json").write_text(text, encoding="utf-8")
    write_csv(snapshot, DATA / "latest.csv")
    GENRE_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")

    # 오래된 보관본 정리
    cutoff = (now - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    for f in SNAP_DIR.glob("????-??-??.json"):
        if f.stem < cutoff:
            f.unlink()
    dates = sorted(f.stem for f in SNAP_DIR.glob("????-??-??.json"))
    (SNAP_DIR / "index.json").write_text(json.dumps(dates), encoding="utf-8")

    failed = [f"{STORES[s]} {LISTS[k]}" for s in STORES for k in LISTS
              if (snapshot["stores"][s].get(k) or {}).get("status") != "ok"]
    log("완료." + (f" 실패: {', '.join(failed)}" if failed else " 6개 목록 모두 성공"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
