"""
週次決算：直近7日分の取引明細をカテゴリ別に集計し、平日・休日の内訳、
カテゴリ別予算オーバーの警告とあわせてLINEに送る。GAS版のsendWeeklyReport()を移植。
送る曜日は WEEKLY_REPORT_WEEKDAY で指定する（1=月〜7=日。「設定」シートで「月・木」のように
複数選ぶと、そのリストになる）。

■ サービス化での変更点
週に複数回送れるようにした。集計する期間は「前回送る曜日の翌日〜今回」に自動で合わせるので、
同じ取引が2回の決算に重複して入ることはない（週1回なら、今までどおり直近7日になる）。
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


def _weekdays() -> list[int]:
    """送る曜日のリスト（1=月〜7=日）。設定が数字1つの場合（旧形式）にも対応する。"""
    value = config.WEEKLY_REPORT_WEEKDAY
    days = value if isinstance(value, (list, tuple)) else [value]
    return sorted({int(d) for d in days if str(d).isdigit() and 1 <= int(d) <= 7})


def _days_since_previous(today: datetime) -> int:
    """今日から見て、1つ前の「送る曜日」が何日前か（週1回なら7、「月・木」の木曜なら3）。"""
    weekday = today.isoweekday()
    gaps = [((weekday - d) % 7) or 7 for d in _weekdays()]
    return min(gaps) if gaps else 7


def build_report(today: datetime) -> str:
    # 集計は「前回の送信時刻の直後〜今」。前回の送信日は、ほぼ前回の決算に含まれているので、
    # 表示上の期間は「前回の翌日〜今日」にする（同じ日付が2つの決算に出て紛らわしくならないように）
    seven_days_ago = today - timedelta(days=_days_since_previous(today))
    label_start = seven_days_ago + timedelta(days=1)

    category_totals: dict[str, int] = {}
    week_total = 0
    transaction_count = 0
    weekday_total = 0
    weekend_total = 0

    for r in sheets_client.get_parsed_detail_rows():
        if not (seven_days_ago < r["date"] <= today):
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
        f"📊 週次決算（{label_start.month}月{label_start.day}日〜{today.month}月{today.day}日）\n"
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

    if now.isoweekday() not in _weekdays():
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
