"""주목할 책 기준 — 수집기(scraper.py)와 보고서(report_data.py)가 함께 쓰는 규칙.

사양서 4장 기준:
  현재 순위 1~10위   → 5계단 이상 상승
  현재 순위 11~30위  → 10계단 이상 상승
  현재 순위 31~100위 → 20계단 이상 상승

상승폭은 서점이 표시한 순위 변동 값을 그대로 쓴다.
- 서점이 'NEW(신규 진입)'로 표시한 책: 이전 순위를 서점이 알려 주면(교보) 그 값으로,
  알려 주지 않으면(예스24) '101위 밖에서 올라온 것'으로 보고 (101 - 현재 순위)를 상승폭으로 본다.
  → 1~81위 신규 진입은 주목할 책, 82~100위 신규 진입은 아님.
- 알라딘은 목록에 순위 변동을 표시하지 않으므로 직전 수집 결과와 비교해 계산한다.
"""


def threshold(rank):
    if rank is None:
        return None
    if rank <= 10:
        return 5
    if rank <= 30:
        return 10
    if rank <= 100:
        return 20
    return None


def effective_rise(item):
    """주목할 책 판정에 쓰는 상승폭. 상승/신규가 아니면 None."""
    t = item.get("change_type")
    if t == "up":
        return item.get("change")
    if t == "new":
        prev = item.get("prev_rank")
        if prev:
            return prev - item["rank"]
        return 101 - item["rank"]
    return None


def apply_rules(item):
    """item에 rise / threshold / multiplier / notable 필드를 채운다."""
    th = threshold(item.get("rank"))
    rise = effective_rise(item)
    item["threshold"] = th
    item["rise"] = rise
    if th and rise is not None and rise > 0:
        item["multiplier"] = round(rise / th, 2)
        item["notable"] = rise >= th
    else:
        item["multiplier"] = None
        item["notable"] = False
    return item


def change_label(item):
    t = item.get("change_type")
    c = item.get("change")
    if t == "new":
        return "NEW"
    if t == "up":
        return f"▲{c}"
    if t == "down":
        return f"▼{abs(c)}"
    if t == "same":
        return "-"
    return "?"
