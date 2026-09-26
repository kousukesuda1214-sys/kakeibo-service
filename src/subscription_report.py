"""
サブスク棚卸し：今月分の「サブスク・エンタメ」カテゴリの取引を一覧にし、
年間換算額・先月からの値上げ検知とあわせてLINEに送る。GAS版のsendSubscriptionListを移植。
月末に1回、日次決算と同じ時間帯で送る。
"""

from datetime import datetime, timedelta

from . import sheets_client, line_client
from .periods import in_report_window, is_last_day_of_month  # noqa: F401（health_checkからのimport互換用）

SUBSCRIPTION_CATEGORY = "サブスク・エンタメ"  # config.CATEGORY_RULES のカテゴリ名と揃えること
PRICE_INCREASE_THRESHOLD = 50  # これ未満の増減は為替の誤差程度としてノイズ扱い


def _base_name(merchant: str) -> str:
    idx = merchant.find("（")
    return (merchant[:idx] if idx != -1 else merchant).strip()


def build_report(today: datetime) -> str:
    target_year, target_month = today.year, today.month
    prev_month_date = datetime(target_year, target_month, 1) - timedelta(days=1)
    prev_year, prev_month = prev_month_date.year, prev_month_date.month

    subscriptions = []
    subscription_total = 0
    current_by_merchant: dict[str, int] = {}
    prev_by_merchant: dict[str, int] = {}

    for r in sheets_client.get_parsed_detail_rows():
        if r["category"] != SUBSCRIPTION_CATEGORY:
            continue
        is_current = r["date"].year == target_year and r["date"].month == target_month
        is_prev = r["date"].year == prev_year and r["date"].month == prev_month
        if not (is_current or is_prev):
            continue

        name = _base_name(r["merchant"])
        if is_current:
            subscriptions.append(r)
            subscription_total += r["amount"]
            current_by_merchant[name] = current_by_merchant.get(name, 0) + r["amount"]
        else:
            prev_by_merchant[name] = prev_by_merchant.get(name, 0) + r["amount"]

    increased = []
    for name, current_amount in current_by_merchant.items():
        prev_amount = prev_by_merchant.get(name)
        if not prev_amount or prev_amount <= 0:
            continue
        diff = current_amount - prev_amount
        if diff >= PRICE_INCREASE_THRESHOLD:
            increased.append((name, prev_amount, current_amount, diff))

    message = (
        f"📋 {today.month}月分 サブスク棚卸しリスト\n"
        f"合計：{subscription_total:,}円（{len(subscriptions)}件）\n\n"
    )

    if not subscriptions:
        message += "この期間、サブスクとして分類された取引はありませんでした。"
    else:
        for s in subscriptions:
            annual = s["amount"] * 12
            message += f"・{s['merchant']}：{s['amount']:,}円（年間換算 {annual:,}円）\n"
        message += "\n不要なものがあれば解約を検討してみてください。"

    if increased:
        message += "\n\n⚠️ 先月より値上がりしているものがあります：\n"
        for name, prev_amount, current_amount, diff in increased:
            message += f"・{name}：{prev_amount:,}円 → {current_amount:,}円（+{diff:,}円）\n"

    return message.strip()


def maybe_send_subscription_list(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    if not is_last_day_of_month(now):
        return False
    if not in_report_window(now):
        return False
    if sheets_client.get_state("subscription_report") == now.strftime("%Y/%m/%d"):
        return False

    message = build_report(now)
    line_client.send_line_message(message)
    sheets_client.set_state("subscription_report", now.strftime("%Y/%m/%d"))
    print("サブスク棚卸しリストを送信しました")
    return True
