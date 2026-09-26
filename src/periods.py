"""
締め期間・支払日・通知の送信時間帯など、「日付の計算」をまとめた共通モジュール。

■ 雛形化での変更点
kakeibo-python（晃介専用版）では「16日始まり〜15日締め」「21時まで」が各ファイルに
直接書かれていたが、雛形では使うカード会社によって締め日が違うため、ここに集約して
config.CLOSING_DAY / PAYMENT_DAY / DAILY_REPORT_HOUR から計算するようにした。

- 末日締めのカード（楽天カードなど）は CLOSING_DAY=31 を設定すればよい。
  2月など31日が無い月は、自動でその月の最終日として扱う。
- 支払日は「締め日の翌月の PAYMENT_DAY 日」として計算する（多くのカードがこの形）。
"""

import calendar
from datetime import datetime, timedelta

from . import config

# 日次決算などを送ってよい時間帯の長さ（DAILY_REPORT_HOUR から何時間以内か）。
# GitHub Actionsの定期実行は数十分遅れることがあるため、少し余裕を持たせている。
REPORT_WINDOW_HOURS = 2


def last_day_of_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def is_last_day_of_month(date: datetime) -> bool:
    return date.day == last_day_of_month(date.year, date.month)


def add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def closing_date_of(year: int, month: int) -> datetime:
    """指定した年月の締め日（その月に日付が無ければ月末に丸める）。"""
    day = min(config.CLOSING_DAY, last_day_of_month(year, month))
    return datetime(year, month, day)


def payment_date_for_closing(closing_date: datetime) -> datetime:
    """締め日から、対応する引落し日（翌月の PAYMENT_DAY 日）を求める。"""
    year, month = add_months(closing_date.year, closing_date.month, 1)
    return datetime(year, month, min(config.PAYMENT_DAY, last_day_of_month(year, month)))


def period_for_closing_month(year: int, month: int) -> tuple[datetime, datetime]:
    """指定した年月に締まる期間を (開始日 0:00, 締め日 23:59:59) で返す。"""
    closing = closing_date_of(year, month)
    prev_year, prev_month = add_months(year, month, -1)
    start = closing_date_of(prev_year, prev_month) + timedelta(days=1)
    end = closing.replace(hour=23, minute=59, second=59)
    return start, end


def closing_month_containing(day: datetime) -> tuple[int, int]:
    """その日が属する締め期間が「何年何月に締まるか」を返す。"""
    if day.date() <= closing_date_of(day.year, day.month).date():
        return day.year, day.month
    return add_months(day.year, day.month, 1)


def is_closing_day(day: datetime) -> bool:
    return day.date() == closing_date_of(day.year, day.month).date()


def get_current_closing_period_info(today: datetime) -> dict:
    """今、進行中の締め期間の情報を計算する。"""
    closing_year, closing_month = closing_month_containing(today)
    period_start, _ = period_for_closing_month(closing_year, closing_month)
    closing_date = closing_date_of(closing_year, closing_month)
    payment_date = payment_date_for_closing(closing_date)

    prev_year, prev_month = add_months(closing_year, closing_month, -1)
    previous_period_start, _ = period_for_closing_month(prev_year, prev_month)

    today_midnight = today.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "period_start": period_start,
        "closing_date": closing_date,
        "payment_date": payment_date,
        "payment_month_date": datetime(payment_date.year, payment_date.month, 1),
        "days_in_period": (closing_date - period_start).days + 1,
        "day_index_in_period": (today_midnight - period_start).days + 1,
        "previous_period_start": previous_period_start,
    }


def in_report_window(now: datetime) -> bool:
    """DAILY_REPORT_HOUR から REPORT_WINDOW_HOURS 時間以内（その日のうち）ならTrue。"""
    start = now.replace(hour=config.DAILY_REPORT_HOUR, minute=0, second=0, microsecond=0)
    end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0)
    end = min(start + timedelta(hours=REPORT_WINDOW_HOURS), end_of_day)
    return start <= now <= end
