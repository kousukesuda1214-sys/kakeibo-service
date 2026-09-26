"""
支払日通知：三井住友カード（Vpass）の「お支払い日のご案内」メールを検知し、
その支払いに対応する締め期間の実績を「取引明細」から集計してLINEで通知する。
海外利用があれば、為替変動の影響（取引時点レート vs 締め日レート）も添える。

■ 雛形化での変更点
- 晃介専用版では「お父様にだけ」送っていたが、雛形では
    ① 必ず自分（LINE_USER_ID）に送る
    ② FATHER_LINE_USER_ID が設定されていれば、その人にも追加で送る
  という形に変更した（変数名は互換性のため FATHER_ のまま。家族に限らず誰でもよい）。
- ENABLE_PAYMENT_NOTICE=false のときは何もしない（Vpass以外のカードの人向け）。
- 締め期間（旧：16日〜15日固定）を periods.py 経由で CLOSING_DAY に追従させた。
"""

import re
from datetime import datetime

from . import config, sheets_client, line_client, parser
from .gmail_client import get_gmail_service, fetch_unprocessed_payment_notice_messages
from .periods import add_months, period_for_closing_month

PAYMENT_DATE_PATTERN = re.compile(r"(\d{1,2})月(\d{1,2})日")
# 店舗名に付いている為替換算メモ「（12.34USD（1USD=150円換算））」から
# 元の外貨金額・通貨コードを取り出す。
_FX_NOTE_PATTERN = re.compile(r"([\d,]+\.?\d*)([A-Z]{3})（1\2=(\d+)円換算）")


def _is_enabled() -> bool:
    """ENABLE_PAYMENT_NOTICE が bool でも文字列でも正しく判定する。"""
    value = getattr(config, "ENABLE_PAYMENT_NOTICE", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "off", "")
    return bool(value)


def _extract_payment_date(body: str, received_at: datetime) -> datetime | None:
    """メール本文から「9月10日」のような支払日を取り出し、年を補って返す。"""
    match = PAYMENT_DATE_PATTERN.search(body)
    if not match:
        return None
    month, day = int(match.group(1)), int(match.group(2))
    year = received_at.year
    if month < received_at.month - 6:  # 例：12月に届いたメールが「1月」なら翌年
        year += 1
    return datetime(year, month, day)


def _closing_period_for_payment(payment_date: datetime) -> dict:
    """支払日から、対応する締め期間（支払月の前月に締まる期間）を逆算する。"""
    closing_year, closing_month = add_months(payment_date.year, payment_date.month, -1)
    period_start, period_end = period_for_closing_month(closing_year, closing_month)
    return {"period_start": period_start, "closing_date": period_end}


def _parse_detail_date(raw) -> datetime | None:
    try:
        return datetime.strptime(str(raw), "%Y/%m/%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def _summarize_period(period_start: datetime, period_end: datetime) -> tuple[int, int]:
    """「取引明細」から、指定期間内の合計金額・件数を集計する。"""
    total = 0
    count = 0
    for row in sheets_client.get_detail_records():
        if len(row) < 4:
            continue
        row_date = _parse_detail_date(row[1])
        if not row_date or not (period_start <= row_date <= period_end):
            continue
        amount_raw = str(row[3]).replace(",", "").strip()
        if not amount_raw:
            continue
        total += int(amount_raw)
        count += 1
    return total, count


def _build_foreign_currency_summary(period_start: datetime, period_end: datetime) -> str:
    """
    期間内の外貨建て取引について、実際に払った円額と、締め日時点のレートで
    計算し直した円額を通貨ごとに比較する。外貨取引が無ければ空文字（返金行は除外）。
    """
    by_currency: dict[str, dict[str, float]] = {}

    for row in sheets_client.get_detail_records():
        if len(row) < 4:
            continue
        merchant, amount_raw = row[2], row[3]
        row_date = _parse_detail_date(row[1])
        if not row_date or not (period_start <= row_date <= period_end):
            continue

        amount_clean = str(amount_raw).replace(",", "").strip()
        if not amount_clean:
            continue
        amount = int(amount_clean)
        if amount <= 0:
            continue

        m = _FX_NOTE_PATTERN.search(str(merchant))
        if not m:
            continue
        foreign_amount = float(m.group(1).replace(",", ""))
        currency_code = m.group(2)

        entry = by_currency.setdefault(currency_code, {"foreign_total": 0.0, "actual_jpy_total": 0})
        entry["foreign_total"] += foreign_amount
        entry["actual_jpy_total"] += amount

    if not by_currency:
        return ""

    total_actual = 0
    total_today = 0
    lines = []
    for code, info in by_currency.items():
        current_rate = parser.get_exchange_rate_to_jpy(code)
        today_jpy = round(info["foreign_total"] * current_rate)
        total_actual += info["actual_jpy_total"]
        total_today += today_jpy
        diff = info["actual_jpy_total"] - today_jpy
        diff_label = f"+{diff:,}円（円安方向）" if diff >= 0 else f"{diff:,}円（円高方向）"
        lines.append(
            f"{code} {info['foreign_total']:,.2f}：実際{info['actual_jpy_total']:,}円／"
            f"締め日レート換算{today_jpy:,}円（{diff_label}）"
        )

    total_diff = total_actual - total_today
    total_diff_label = (
        f"締め日時点のレートより{total_diff:,}円多く払った計算です"
        if total_diff >= 0 else
        f"締め日時点のレートより{abs(total_diff):,}円少なく済んだ計算です"
    )
    return "【海外利用の為替影響】\n" + "\n".join(lines) + f"\n合計：{total_diff_label}"


def _send_to_recipients(message: str) -> None:
    line_client.send_line_message(message)  # 自分（LINE_USER_ID）には必ず送る
    print("支払日通知を送信しました")
    if config.FATHER_LINE_USER_ID:
        line_client.send_line_message(message, user_id=config.FATHER_LINE_USER_ID)
        print("支払日通知を追加の送り先（FATHER_LINE_USER_ID）にも送信しました")


def process_payment_notice_email() -> bool:
    """未処理の支払日案内メールがあれば処理する。処理した場合はTrueを返す。"""
    if not _is_enabled():
        return False

    service = get_gmail_service()
    processed_any = False

    for msg in fetch_unprocessed_payment_notice_messages(service):
        processed_any = True
        received_at = datetime.now()  # 概算でよい（年またぎ判定にのみ使用）
        payment_date = _extract_payment_date(msg["body"], received_at)
        if not payment_date:
            print(f"支払日を抽出できませんでした: {msg['subject']}")
            continue

        period = _closing_period_for_payment(payment_date)
        closing_total, closing_count = _summarize_period(period["period_start"], period["closing_date"])

        payment_month_date = datetime(payment_date.year, payment_date.month, 1)
        this_month_budget_info = sheets_client.get_monthly_budget(payment_month_date)
        this_month_budget = this_month_budget_info["budget"] if this_month_budget_info else 0

        if not this_month_budget_info:
            this_month_budget_info = sheets_client.ensure_monthly_budget_row(payment_month_date)

        if this_month_budget_info:
            sheets_client.set_closing_actual(this_month_budget_info["row"], closing_total)

        next_year, next_month = add_months(payment_month_date.year, payment_month_date.month, 1)
        next_month_date = datetime(next_year, next_month, 1)
        next_budget_info = sheets_client.get_monthly_budget(next_month_date)
        if not next_budget_info:
            next_budget_info = sheets_client.ensure_monthly_budget_row(next_month_date)
        next_month_budget = next_budget_info["budget"] if next_budget_info else None

        diff = closing_total - this_month_budget
        diff_abs = abs(diff)
        diff_judge = f"{diff_abs:,}円のオーバーです。" if diff >= 0 else f"{diff_abs:,}円の余裕です。"

        message = (
            f"{payment_date.year}/{payment_date.month}/{payment_date.day}"
            f"（{config.SENDER_NAME_LABEL}） 引落し分\n"
            f"計{closing_total:,}円 確認しました。\n"
            f"計画{this_month_budget:,}円に対し実績{closing_total:,}円\n"
            f"{'プラス' if diff >= 0 else 'マイナス'}{diff_abs:,}円でした。\n"
            f"{closing_count}件確認しました。\n"
            f"計画は、{this_month_budget:,}円でしたので、{diff_judge}\n"
        )
        if next_month_budget is not None:
            message += f"次月は {next_month_budget:,}円の計画です。\n"
        else:
            message += "次月の計画額が「予算計画」シートに未入力です。\n"

        fx_summary = _build_foreign_currency_summary(period["period_start"], period["closing_date"])
        if fx_summary:
            message += f"\n{fx_summary}\n"

        message += "\nよろしくお願いします。"
        _send_to_recipients(message)

    return processed_any
