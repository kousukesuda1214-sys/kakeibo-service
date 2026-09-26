"""
週次決算：直近7日分の取引明細をカテゴリ別に集計し、平日・休日の内訳、
カテゴリ別予算オーバーの警告とあわせてLINEに送る。GAS版のsendWeeklyReport()を移植。
送る曜日は WEEKLY_REPORT_WEEKDAY（1=月〜7=日）で指定する。
"""

from datetime import datetime, timedelta

from . import config, sheets_client, line_client
from .periods import get_current_closing_period_info, in_report_window


def _bar_chart(percent: int) -> str:
    filled = round(percent / 10)
    return "■" * filled + "□" * (10 - filled)


def _build_category_budget_section(today: datetime) -> str:
    """「カテゴリ予算」シートと、進行中の締め期間の実績を比較し、予算オーバーの
    カテゴリがあればセクション文を返す（無ければ空文字）。"""
    budgets = sheets_client.get_category_budgets()
    if not budgets:
        return ""

    period = get_current_closing_period_info(today)
    category_totals: dict[str, int] = {}
    for r in sheets_client.get_parsed_detail_rows():
        if not (period["period_start"] <= r["date"] <= today):
            continue
        category_totals[r["category"]] = category_totals.get(r["category"], 0) + r["amount"]

    over_budget = []
    for category, budget in budgets.items():
        actual = category_totals.get(category, 0)
        if actual > budget:
            over_budget.append((category, budget, actual, actual - budget))

    if not over_budget:
        return ""

    lines = [
        "【カテゴリ別予算オーバー】",
        f"（↑の内訳とは期間が違います：{period['period_start'].month}/{period['period_start'].day}〜現在、締めまでの累計）",
    ]
    for category, budget, actual, over in over_budget:
        lines.append(f"⚠️ {category}：{actual:,}円／予算{budget:,}円（+{over:,}円）")
    return "\n".join(lines)


def build_report(today: datetime) -> str:
    seven_days_ago = today - timedelta(days=7)

    category_totals: dict[str, int] = {}
    week_total = 0
    transaction_count = 0
    weekday_total = 0
    weekend_total = 0

    for r in sheets_client.get_parsed_detail_rows():
        if not (seven_days_ago <= r["date"] <= today):
            continue
        category_totals[r["category"]] = category_totals.get(r["category"], 0) + r["amount"]
        week_total += r["amount"]
        transaction_count += 1
        if r["date"].weekday() >= 5:  # 5=土, 6=日
            weekend_total += r["amount"]
        else:
            weekday_total += r["amount"]

    sorted_categories = sorted(category_totals.items(), key=lambda kv: kv[1], reverse=True)

    message = (
        f"📊 週次決算（{seven_days_ago.month}月{seven_days_ago.day}日〜{today.month}月{today.day}日）\n"
        f"合計利用額：{week_total:,}円（{transaction_count}件）\n\n"
        "【カテゴリ別内訳】\n"
    )

    if not sorted_categories:
        message += "この期間の取引はありませんでした。"
    else:
        for category, amount in sorted_categories:
            percent = round(amount / week_total * 100) if week_total > 0 else 0
            message += f"{category}：{amount:,}円（{percent}%）\n{_bar_chart(percent)}\n"

    if week_total > 0:
        weekday_percent = round(weekday_total / week_total * 100)
        weekend_percent = 100 - weekday_percent
        message += (
            "\n【平日・休日の内訳】\n"
            f"平日：{weekday_total:,}円（{weekday_percent}%）\n"
            f"休日：{weekend_total:,}円（{weekend_percent}%）"
        )

    category_budget_section = _build_category_budget_section(today)
    if category_budget_section:
        message += f"\n\n{category_budget_section}"

    return message.strip()


def maybe_send_weekly_report(now: datetime | None = None) -> bool:
    """指定した曜日の送信時間帯に、その日まだ送っていなければ週次決算をLINEに送る。"""
    now = now or datetime.now()

    if now.isoweekday() != config.WEEKLY_REPORT_WEEKDAY:
        return False
    if not in_report_window(now):
        return False
    if sheets_client.get_state("weekly_report") == now.strftime("%Y/%m/%d"):
        return False

    message = build_report(now)
    line_client.send_line_message(message)
    sheets_client.set_state("weekly_report", now.strftime("%Y/%m/%d"))
    print("週次決算を送信しました")
    return True
