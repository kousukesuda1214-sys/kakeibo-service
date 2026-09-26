"""
日次決算：進行中の締め期間の累計・予算比較をLINEに送る。GAS版のsendDailyReport()を移植。

■ 雛形化での変更点
- 締め期間（旧：16日〜翌月15日固定）の計算を periods.py に移し、CLOSING_DAY に合わせて
  自動計算するようにした。get_current_closing_period_info は他のファイルからの
  import互換のため、このファイルからも引き続き使える。
- 送信時間帯の終わり（旧：21時固定）を DAILY_REPORT_HOUR から2時間以内に変更。
- 前回サイクル同時点比の計算で、月末締めなどのときに日付計算が失敗する問題を回避。
"""

from datetime import datetime, timedelta

from . import config, sheets_client, line_client
from .periods import get_current_closing_period_info, in_report_window  # noqa: F401（互換用の再公開）


def _parse_date(cell: str):
    for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(cell, fmt)
        except (ValueError, TypeError):
            continue
    return None


def _to_int(value) -> int:
    try:
        return int(str(value).replace(",", "").strip() or 0)
    except ValueError:
        return 0


def _already_sent_today(today: datetime) -> bool:
    return sheets_client.get_last_daily_report_date() == today.strftime("%Y/%m/%d")


def _mark_sent(today: datetime) -> None:
    sheets_client.set_last_daily_report_date(today.strftime("%Y/%m/%d"))


def _get_previous_period_to_date_total(records, period: dict) -> int:
    prev_start = period["previous_period_start"]
    prev_target_end = prev_start + timedelta(days=period["day_index_in_period"] - 1)

    total = 0
    for row in records:
        if len(row) < 3:
            continue
        row_date = _parse_date(row[1])
        if not row_date:
            continue
        if prev_start <= row_date <= prev_target_end:
            total += _to_int(row[2])
    return total


def build_report(today: datetime):
    period = get_current_closing_period_info(today)

    budget_info = sheets_client.get_monthly_budget(period["payment_month_date"])
    carry_forward_note = ""
    if not budget_info:
        auto_created = sheets_client.ensure_monthly_budget_row(period["payment_month_date"])
        if auto_created:
            budget_info = auto_created
            label = f"{period['payment_month_date'].year}年{period['payment_month_date'].month}月"
            carry_forward_note = (
                f"📋 直近の月と同じ予算（{auto_created['budget']:,}円）を{label}分として自動で追加しました。"
                f"金額が違う場合は「予算計画」シートのC列を書き換えてください。\n\n"
            )

    using_sample_budget = budget_info is None
    period_budget = budget_info["budget"] if budget_info else config.SAMPLE_BUDGET_AMOUNT

    records = sheets_client.get_log_records()
    today_spend = 0
    period_total = 0
    for row in records:
        if len(row) < 3:
            continue
        row_date = _parse_date(row[1])
        if not row_date:
            continue
        amount = _to_int(row[2])
        if period["period_start"] <= row_date <= today:
            period_total += amount
            if row_date.date() == today.date():
                today_spend = amount

    budget_to_date = round(period_budget * period["day_index_in_period"] / period["days_in_period"])
    budget_diff = budget_to_date - period_total
    remaining = period_budget - period_total
    avg_per_day = period_total / period["day_index_in_period"] if period["day_index_in_period"] > 0 else 0
    forecast = round(avg_per_day * period["days_in_period"])

    prev_total = _get_previous_period_to_date_total(records, period)
    vs_prev = period_total - prev_total
    vs_prev_line = (
        f"📆 前回サイクル同時点比：{'+' if vs_prev >= 0 else ''}{vs_prev:,}円（前回同時点は{prev_total:,}円）\n"
        if prev_total > 0 else ""
    )

    consumption_ratio = period_total / period_budget if period_budget > 0 else 0
    if consumption_ratio >= 1.2:
        alert_line = "🔴 予算を大幅にオーバーしています（120%以上）！見直しが必要です。\n\n"
    elif consumption_ratio >= 1.0:
        alert_line = "⚠️ 予算オーバーしています！\n\n"
    elif consumption_ratio >= 0.8:
        alert_line = "🟡 予算の80%を消化しました。そろそろ気をつけましょう。\n\n"
    elif consumption_ratio >= 0.5:
        alert_line = "🟢 予算の半分を消化しました。\n\n"
    else:
        alert_line = ""

    if using_sample_budget:
        label = f"{period['payment_month_date'].year}年{period['payment_month_date'].month}月"
        sample_note = (
            f"⚠️「予算計画」シートに{label}分の行が無いため、サンプル予算"
            f"（{config.SAMPLE_BUDGET_AMOUNT:,}円）で計算しています。実際の年月・予算額を入力してください。\n\n"
        )
    else:
        sample_note = carry_forward_note
        sheets_client.update_budget_actuals(budget_info["row"], period_total, period_budget - period_total)

    pace_line = (
        f"📊 ペース：予算より{budget_diff:,}円 余裕あり\n"
        if budget_diff >= 0 else
        f"📊 ペース：予算より{abs(budget_diff):,}円 押し気味\n"
    )
    remaining_line = (
        f"💰 締めまであと使える金額：{remaining:,}円\n"
        if remaining >= 0 else
        f"💸 予算超過額：{abs(remaining):,}円（締めの予算は使い切っています）\n"
    )

    message = (
        f"{sample_note}"
        f"{alert_line}"
        f"{today.month}月{today.day}日 日次決算\n"
        f"期間：{period['period_start'].month}/{period['period_start'].day}〜"
        f"（{period['closing_date'].month}/{period['closing_date'].day}締め）\n\n"
        f"💳 本日：{today_spend:,}円\n"
        f"📅 累計：{period_total:,}円　／　予算：{period_budget:,}円\n"
        f"🎯 今日までの目安：{budget_to_date:,}円\n"
        f"{pace_line}"
        f"{remaining_line}"
        f"📈 このペースが続いた場合の締め時点予測：{forecast:,}円\n"
        f"{vs_prev_line}"
    ).strip()

    return message


def maybe_send_daily_report(now: datetime | None = None) -> bool:
    """送信時間帯に、その日まだ送っていなければ日次決算をLINEに送る。"""
    now = now or datetime.now()
    if not in_report_window(now):
        return False
    if _already_sent_today(now):
        return False

    message = build_report(now)
    line_client.send_line_message(message)
    _mark_sent(now)
    print("日次決算を送信しました")
    return True
