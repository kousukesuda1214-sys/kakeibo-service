"""
LINEのリッチメニュー（「今日の決算」「今月のカテゴリ別」「設定」など）で表示するための、
利用者の最新の数字（スナップショット）を作る。

30分おきの実行の最後に作って、runner.py 経由で中継役（GAS）に預けておく。
利用者がLINEのボタンを押したら、中継役がこの数字でカードを作って、すぐに「返信」する
（返信はLINEの月の送信数に数えられないので、何回押しても無料）。

スプレッドシートからは読み取るだけで、何も書き込まない。
"""

from datetime import datetime

from . import config, sheets_client
from .daily_report import _parse_date, _to_int
from .periods import get_current_closing_period_info
from .settings import SETTINGS, _current_value, _display

MAX_CATEGORIES = 8


def build(now: datetime | None = None) -> dict:
    now = now or datetime.now()
    period = get_current_closing_period_info(now)

    budget_info = sheets_client.get_monthly_budget(period["payment_month_date"])
    budget = budget_info["budget"] if budget_info else config.SAMPLE_BUDGET_AMOUNT

    # 締め期間の累計と、今日の利用額（「ログ」シートから）
    today_spend = 0
    total = 0
    for row in sheets_client.get_log_records():
        if len(row) < 3:
            continue
        row_date = _parse_date(row[1])
        if not row_date or not (period["period_start"] <= row_date <= now):
            continue
        amount = _to_int(row[2])
        total += amount
        if row_date.date() == now.date():
            today_spend += amount

    # 締め期間のカテゴリ別（「取引明細」シートから）
    by_category: dict[str, int] = {}
    count = 0
    for r in sheets_client.get_parsed_detail_rows():
        if period["period_start"] <= r["date"] <= now:
            by_category[r["category"]] = by_category.get(r["category"], 0) + r["amount"]
            count += 1
    categories = sorted(by_category.items(), key=lambda kv: kv[1], reverse=True)
    top = categories[:MAX_CATEGORIES]
    rest = sum(amount for _, amount in categories[MAX_CATEGORIES:])
    if rest:
        top.append(("そのほか", rest))

    days = period["days_in_period"]
    day_index = max(period["day_index_in_period"], 1)
    budget_to_date = round(budget * day_index / days) if days else 0

    ss = sheets_client._spreadsheet()
    gids = {ws.title: ws.id for ws in sheets_client._call_with_retry(ss.worksheets)}

    return {
        "updated_at": now.strftime("%-m/%-d %H:%M"),
        "period": (
            f"{period['period_start'].month}/{period['period_start'].day}〜"
            f"{period['closing_date'].month}/{period['closing_date'].day}締め"
        ),
        "day_index": day_index,
        "days": days,
        "today": today_spend,
        "total": total,
        "budget": budget,
        "budget_is_sample": budget_info is None,
        "budget_to_date": budget_to_date,
        "remaining": budget - total,
        "forecast": round(total / day_index * days) if days else total,
        "count": count,
        "categories": [{"name": name, "amount": amount} for name, amount in top],
        "sheet_id": config.GOOGLE_SHEET_ID,
        "gids": {
            "settings": gids.get("設定"),
            "budget": gids.get(config.SHEET_BUDGET),
            "detail": gids.get(config.SHEET_DETAIL),
            "category_rules": gids.get(sheets_client.SHEET_CATEGORY_RULES),
        },
        "settings": [
            {"label": label, "value": _display(kind, _current_value(attr))}
            for attr, label, kind, _ in SETTINGS
        ],
    }
