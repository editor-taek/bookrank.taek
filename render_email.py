"""보고서 메일 HTML 만들기.

report_data.py가 만든 report.json + Claude가 쓴 원인 분석 analysis.json → 메일 HTML·일반 텍스트.
메일 프로그램(네이버·지메일)은 <style>을 지우는 경우가 많아 모든 꾸밈을 태그에 직접 넣는다.

analysis.json 형식:
{
  "summary": ["한 줄", "한 줄", "한 줄"],
  "weekly": [{"key": "<picks의 key>", "status": "found|unknown|see_weekly",
              "cause": "2~4문장", "sources": [{"title": "...", "url": "https://..."}]}],
  "daily":  [ ...같은 형식... ],
  "system_note": ""
}

실행: python render_email.py --report /tmp/report.json --analysis /tmp/analysis.json \
        --html /tmp/email.html --text /tmp/email.txt
"""
import argparse
import html
import json
from pathlib import Path

STORES = {"kyobo": "교보", "yes24": "예스24", "aladin": "알라딘"}
STORES_FULL = {"kyobo": "교보문고", "yes24": "예스24", "aladin": "알라딘"}

# 색
INK, SUB, LINE, BG = "#1d1d1f", "#86868b", "#ececf0", "#f3f4f6"
UP, DOWN, HOT, HOT_INK = "#e5484d", "#3e63dd", "#fff4cc", "#7a5200"
TEEN_BG, TEEN_INK = "#e8f1ff", "#0b5cad"
FONT = "-apple-system,BlinkMacSystemFont,'Apple SD Gothic Neo','Malgun Gothic','Noto Sans KR',sans-serif"


def e(s):
    return html.escape(str(s if s is not None else ""))


def short(label):
    """월요일 '(금→월)' 꼬리표는 상단 안내로 대신하고 칸에서는 뺀다."""
    return str(label or "").replace("(금→월)", "")


def chg_color(label, ctype=None):
    if ctype == "down" or str(label).startswith("▼"):
        return DOWN
    if ctype in ("up", "new") or str(label).startswith(("▲", "NEW")):
        return UP
    return SUB


def card(inner, pad="20px 20px"):
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="background:#ffffff;border-radius:14px;margin:0 0 14px 0;border:1px solid {LINE}">'
            f'<tr><td style="padding:{pad}">{inner}</td></tr></table>')


def h2(text, sub=""):
    s = f'<span style="font-size:13px;color:{SUB};font-weight:normal;margin-left:6px">{e(sub)}</span>' if sub else ""
    return f'<div style="font-size:19px;font-weight:700;color:{INK};margin:26px 4px 10px">{e(text)}{s}</div>'


def label(text):
    return f'<div style="font-size:12px;font-weight:700;color:{SUB};letter-spacing:.5px;margin:0 0 10px">{e(text)}</div>'


def chip(text, color=INK, bg=BG):
    return (f'<span style="display:inline-block;font-size:12px;line-height:18px;padding:2px 8px;border-radius:999px;'
            f'background:{bg};color:{color};margin:0 4px 4px 0;white-space:nowrap">{text}</span>')


# ------------------------------------------------------------------ 원인 분석 카드
STATUS = {"found": ("원인 확인", "#0a7a3d", "#e3f6ea"),
          "unknown": ("원인 불명", "#6e6e73", "#efeff2"),
          "see_weekly": ("위 주간 분석 참고", TEEN_INK, TEEN_BG)}


def analysis_card(n, g, a):
    a = a or {"status": "unknown", "cause": "원인 조사 결과가 없습니다.", "sources": []}
    st_text, st_ink, st_bg = STATUS.get(a.get("status"), STATUS["unknown"])
    hits = "".join(chip(f'<b>{STORES[h["store"]]}</b> {h["rank"]}위 '
                        f'<span style="color:{chg_color(h["change_label"])};font-weight:700">{e(short(h["change_label"]))}</span>')
                   for h in g["hits"])
    src = ""
    if a.get("sources"):
        src = ('<div style="margin-top:10px;font-size:12px;line-height:1.7">' +
               "<br>".join(f'<a href="{e(s.get("url"))}" style="color:{SUB};text-decoration:underline">'
                           f'{e(s.get("title") or s.get("url"))}</a>' for s in a["sources"][:4]) + "</div>")
    inner = (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
        f'<td width="30" valign="top" style="font-size:20px;font-weight:800;color:{UP};padding-top:1px">{n}</td>'
        f'<td valign="top">'
        f'<a href="{e(g.get("link"))}" style="font-size:16px;font-weight:700;color:{INK};text-decoration:none;line-height:1.4">{e(g["title"])}</a>'
        f'<div style="font-size:13px;color:{SUB};margin:3px 0 10px">{e(g["author"])} · {e(g["publisher"])}</div>'
        f'<div style="margin-bottom:8px">{hits}{chip(st_text, st_ink, st_bg)}</div>'
        f'<div style="font-size:14px;line-height:1.7;color:#3a3a3c">{e(a.get("cause"))}</div>{src}'
        f'</td></tr></table>')
    return card(inner, "18px 18px")


# ------------------------------------------------------------------ 서점 비교 표
def store_cell(c, monday):
    if not c:
        return f'<td align="center" style="padding:10px 2px;border-top:1px solid {LINE};color:#c7c7cc;font-size:13px">–</td>'
    bg = f"background:{HOT};" if c["notable"] else ""
    rank_color = INK if c["notable"] else SUB
    chg = short(c.get("change_label"))
    extra = ""
    if monday and c["notable"] and c.get("fri_rank") is not None:
        extra = f'<div style="font-size:11px;color:{SUB}">금 {c["fri_rank"]}위</div>'
    return (f'<td align="center" valign="middle" style="{bg}padding:8px 2px;border-top:1px solid {LINE}">'
            f'<div style="font-size:15px;font-weight:700;color:{rank_color}">{c["rank"]}</div>'
            f'<div style="font-size:11px;font-weight:700;color:{chg_color(chg, c.get("change_type"))};white-space:nowrap">{e(chg)}</div>'
            f'{extra}</td>')


def board_table(board, monday):
    head = (f'<tr><td style="font-size:11px;color:{SUB};padding:0 0 8px">책</td>' +
            "".join(f'<td width="17%" align="center" style="font-size:11px;color:{SUB};padding:0 0 8px">{n}</td>'
                    for n in STORES.values()) + "</tr>")
    rows = ""
    for b in board:
        rows += (f'<tr><td valign="middle" style="padding:9px 6px 9px 0;border-top:1px solid {LINE}">'
                 f'<a href="{e(b["link"])}" style="font-size:14px;font-weight:600;color:{INK};text-decoration:none;line-height:1.35">{e(b["title"])}</a>'
                 f'<div style="font-size:12px;color:{SUB};margin-top:2px">{e(b["author"])} · {e(b["publisher"])}</div></td>'
                 + "".join(store_cell(b["cells"].get(s), monday) for s in STORES) + "</tr>")
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{head}{rows}</table>'


def teen_table(teen):
    if not teen:
        return f'<div style="font-size:14px;color:{SUB}">오늘은 종합 100위 안에 청소년 책이 없습니다.</div>'
    head = (f'<tr><td style="font-size:11px;color:{SUB};padding:0 0 8px">책</td>' +
            "".join(f'<td width="17%" align="center" style="font-size:11px;color:{SUB};padding:0 0 8px">{n}</td>'
                    for n in STORES.values()) + "</tr>")
    rows = ""
    for t in teen:
        cells = ""
        for s in STORES:
            c = t["cells"].get(s)
            if not c:
                cells += f'<td align="center" style="border-top:1px solid {LINE};color:#c7c7cc;font-size:13px">–</td>'
                continue
            lines = ""
            for kind, kn in (("weekly", "주"), ("daily", "일")):
                if kind in c:
                    x = c[kind]
                    lines += (f'<div style="font-size:12px;white-space:nowrap;color:{INK}"><span style="color:{SUB}">{kn}</span> '
                              f'<b>{x["rank"]}</b> <span style="color:{chg_color(x["change_label"], x.get("change_type"))};font-size:11px">{e(x["change_label"])}</span></div>')
            cells += f'<td align="center" style="padding:8px 2px;border-top:1px solid {LINE}">{lines}</td>'
        rows += (f'<tr><td style="padding:9px 6px 9px 0;border-top:1px solid {LINE}">'
                 f'<a href="{e(t["link"])}" style="font-size:14px;font-weight:600;color:{INK};text-decoration:none">{e(t["title"])}</a>'
                 f'<div style="font-size:12px;color:{SUB};margin-top:2px">{e(t["author"])} · {e(t["publisher"])}</div></td>{cells}</tr>')
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{head}{rows}</table>'


# ------------------------------------------------------------------ 전체
def render(r, a):
    monday = r.get("monday_mode")
    y, m, d = r["date"].split("-")
    page = r["page_url"]
    parts = []

    # 머리
    parts.append(
        f'<div style="padding:8px 4px 18px">'
        f'<div style="font-size:13px;color:{SUB};font-weight:600">서점 3사 베스트셀러 · 아침 보고</div>'
        f'<div style="font-size:28px;font-weight:800;color:{INK};margin:4px 0 2px">{int(m)}월 {int(d)}일 ({e(r["weekday"])})</div>'
        f'<div style="font-size:12px;color:{SUB}">{e(r.get("collected_at") or "")} 수집</div></div>')

    # 경고·안내
    notes = []
    if not r.get("snapshot_found"):
        notes.append(("⚠ 오늘 순위 수집 실패 — 수집 파일이 없습니다. 깃허브 Actions 실행 기록을 확인해 주세요.", True))
    fails = [c for c in r["collection"].values() if c["status"] != "ok"]
    if r.get("snapshot_found") and fails:
        notes.append(("⚠ 수집 실패: " + ", ".join(f"{c['store']} {c['list']}" for c in fails), True))
    if monday:
        notes.append(("월요일 보고서 — 금요일 → 월요일 주말 변동을 모아 정리했습니다. 표의 '금 ○위'는 금요일 순위입니다.", False))
    for c in r["collection"].values():
        if c["status"] == "ok" and not c.get("updated", True):
            notes.append((f"{c['store']} {c['list']}은 오늘 갱신되지 않아 변동 없음으로 처리했습니다.", False))
    for text, bad in notes:
        bg, ink = ("#fdecec", "#b42318") if bad else ("#fff8e1", "#7a5200")
        parts.append(f'<div style="background:{bg};color:{ink};font-size:13px;line-height:1.6;'
                     f'padding:10px 14px;border-radius:10px;margin:0 0 10px">{e(text)}</div>')

    # 요약
    summary = a.get("summary") or []
    if summary:
        lis = "".join(f'<tr><td width="18" valign="top" style="color:{UP};font-weight:800;font-size:15px;line-height:1.6">·</td>'
                      f'<td style="font-size:15px;line-height:1.6;color:{INK};padding-bottom:6px">{e(s)}</td></tr>'
                      for s in summary[:3])
        parts.append(card(label("오늘의 한눈 요약") +
                          f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{lis}</table>'))

    # 주간 / 일간
    for kind, sec in r["sections"].items():
        nc = sec["notable_counts"]
        parts.append(h2(sec["list_name"], " · ".join(f"{STORES[s]} {nc.get(s, 0)}" for s in STORES) + " 권 주목"))
        ana = {x.get("key"): x for x in a.get(kind, [])}
        if sec["picks"]:
            parts.append(f'<div style="font-size:13px;font-weight:700;color:{SUB};margin:0 4px 8px">왜 올랐나 — 원인 분석</div>')
            for i, g in enumerate(sec["picks"], 1):
                parts.append(analysis_card(i, g, ana.get(g["key"])))
        else:
            parts.append(card(f'<div style="font-size:14px;color:{SUB}">오늘은 원인 조사 대상이 없습니다.</div>'))
        if sec["board"]:
            more = sec["board_total"] - len(sec["board"])
            foot = (f'<div style="margin-top:12px;font-size:13px"><a href="{e(page)}" style="color:{TEEN_INK};text-decoration:none">'
                    f'외 {more}권은 웹페이지에서 보기 →</a></div>') if more > 0 else ""
            legend = (f'<div style="font-size:11px;color:{SUB};margin-top:10px">'
                      f'<span style="background:{HOT};padding:1px 6px;border-radius:4px">노란 칸</span> 그 서점에서 주목할 책 · '
                      f'회색 숫자는 현재 순위만 · – 는 100위 밖</div>')
            parts.append(card(label("주목할 책 한눈에 (서점별 순위)") + board_table(sec["board"], monday) + legend + foot,
                              "18px 16px"))

    # 청소년
    parts.append(h2("청소년", f"{len(r.get('teen', []))}권"))
    parts.append(card(label("종합 100위 안 청소년 책 (주=주간, 일=일간)") + teen_table(r.get("teen", [])), "18px 16px"))

    # 끝
    parts.append(
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:18px 0 8px"><tr><td align="center">'
        f'<a href="{e(page)}" style="display:inline-block;background:{INK};color:#ffffff;text-decoration:none;'
        f'font-size:15px;font-weight:700;padding:13px 26px;border-radius:999px">전체 순위 보기</a></td></tr></table>')
    if a.get("system_note"):
        parts.append(f'<div style="font-size:12px;color:{SUB};margin:14px 4px">시스템 메모: {e(a["system_note"])}</div>')
    parts.append(f'<div style="font-size:11px;color:#aeaeb2;line-height:1.6;margin:16px 4px 0">'
                 f'주목 기준: 1~10위 5계단↑ · 11~30위 10계단↑ · 31~100위 20계단↑ (서점 표시 변동 기준). '
                 f'NEW는 100위 밖에서 진입으로 계산. 알라딘은 변동 표시가 없어 전날 수집분과 비교.</div>')

    body = "".join(parts)
    return (f'<!DOCTYPE html><html><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
            f'<body style="margin:0;padding:0;background:{BG}">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{BG}"><tr><td align="center">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:640px;font-family:{FONT}">'
            f'<tr><td style="padding:22px 14px 30px">{body}</td></tr></table></td></tr></table></body></html>')


def render_text(r, a):
    out = [r["subject"], ""]
    for s in a.get("summary") or []:
        out.append(f"- {s}")
    for kind, sec in r["sections"].items():
        out += ["", f"[{sec['list_name']}] 원인 분석"]
        ana = {x.get("key"): x for x in a.get(kind, [])}
        for i, g in enumerate(sec["picks"], 1):
            x = ana.get(g["key"]) or {}
            hits = ", ".join(f"{STORES[h['store']]} {h['rank']}위 {short(h['change_label'])}" for h in g["hits"])
            out.append(f"{i}. {g['title']} ({g['author']} · {g['publisher']}) — {hits}")
            out.append(f"   {x.get('cause', '원인 조사 결과 없음')}")
        if not sec["picks"]:
            out.append("오늘은 원인 조사 대상이 없습니다.")
    out += ["", f"청소년 책 {len(r.get('teen', []))}권 · 전체 순위: {r['page_url']}"]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default="/tmp/report.json")
    ap.add_argument("--analysis", default="/tmp/analysis.json")
    ap.add_argument("--html", default="/tmp/email.html")
    ap.add_argument("--text", default="/tmp/email.txt")
    o = ap.parse_args()
    r = json.loads(Path(o.report).read_text(encoding="utf-8"))
    p = Path(o.analysis)
    a = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    Path(o.html).write_text(render(r, a), encoding="utf-8")
    Path(o.text).write_text(render_text(r, a), encoding="utf-8")
    print(f"subject: {r['subject']}\nhtml: {o.html}\ntext: {o.text}")


if __name__ == "__main__":
    main()
