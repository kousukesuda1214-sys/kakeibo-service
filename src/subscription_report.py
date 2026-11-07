"""
サブスク棚卸し：今月請求があった「サブスクと見てよいもの」を一覧にし、
年間換算額・先月からの値上げ検知とあわせてLINEに送る。GAS版のsendSubscriptionListを移植。
月末に1回、日次決算と同じ時間帯で送る。

■ 2026/11 変更：カテゴリではなく、請求の実績で見分ける
以前は「サブスク・エンタメ」カテゴリの取引をすべて載せていたので、AIが店名から推測を誤ると、
1回だけの利用（クリーニング代など）までサブスクとして年間換算していた。次のどれかに当てはまるものだけを載せる：
- 店名に定額の言葉がある（「/月」「Subscription」など）か、定額サービスとして有名な名前
- 同じ店から同じ金額が、約1か月（または約1年）あけて請求されている（カテゴリは問わない）
- 「サブスク・エンタメ」カテゴリで、約1か月あけてほぼ同じ金額（外貨建ては為替で少し変わるため）
「Visa加盟店」「Suica」のように店名が隠れているものは、別の店をまとめて同じ名前に見えるので、対象にしない。
1回だけの利用は載せない（本当に新しいサブスクなら、2回目の請求があった月から載る）。
"""

import unicodedata
from datetime import datetime, timedelta

from . import sheets_client, line_client
from .periods import in_report_window, is_last_day_of_month  # noqa: F401（health_checkからのimport互換用）

SUBSCRIPTION_CATEGORY = "サブスク・エンタメ"  # config.CATEGORY_RULES のカテゴリ名と揃えること
PRICE_INCREASE_THRESHOLD = 50  # これ未満の増減は為替の誤差程度としてノイズ扱い

MONTHLY, YEARLY = "月額", "年額"
MONTHLY_GAP_DAYS = (25, 35)   # 毎月の請求とみなす間隔
YEARLY_GAP_DAYS = (350, 380)  # 毎年の請求とみなす間隔
NEAR_AMOUNT_RATE = 0.05       # 「ほぼ同じ金額」の幅（外貨建ての為替の違い）

YEARLY_WORDS = ("/年", "年額", "ANNUAL", "YEARLY")
MONTHLY_WORDS = ("/月", "月額", "MONTHLY", "SUBSCRIPTION", "サブスク", "定期購入")
KNOWN_SERVICES = (
    "NETFLIX", "SPOTIFY", "AMAZON PRIME", "YOUTUBE PREMIUM", "DISNEY+", "DISNEY PLUS", "U-NEXT", "HULU", "DAZN",
    "ABEMA", "APPLE MUSIC", "ICLOUD", "ADOBE", "OPENAI", "CHATGPT", "ANTHROPIC", "MICROSOFT 365",
)
MASKED_NAMES = ("VISA加盟店", "SUICA", "PASMO", "ICOCA", "PAYPAY利用", "SQUARE", "楽天カード", "GOOGLE PLAY APP")


def _base_name(merchant: str) -> str:
    idx = merchant.find("（")
    return (merchant[:idx] if idx != -1 else merchant).strip()


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).upper()


def _gap_cycle(days: int) -> str | None:
    if MONTHLY_GAP_DAYS[0] <= days <= MONTHLY_GAP_DAYS[1]:
        return MONTHLY
    if YEARLY_GAP_DAYS[0] <= days <= YEARLY_GAP_DAYS[1]:
        return YEARLY
    return None


def _subscription_cycle(charges: list[dict]) -> str | None:
    """同じ店の請求の一覧（古い順）から、月額・年額・サブスクではない（None）を判定する。"""
    latest = _normalize(charges[-1]["merchant"])
    if any(name in latest for name in MASKED_NAMES):
        return None
    if any(word in latest for word in YEARLY_WORDS):
        return YEARLY
    if any(word in latest for word in MONTHLY_WORDS + KNOWN_SERVICES):
        return MONTHLY

    last_seen_by_amount: dict[int, datetime] = {}
    cycle = None
    for charge in charges:
        previous = last_seen_by_amount.get(charge["amount"])
        if previous:
            cycle = _gap_cycle((charge["date"] - previous).days) or cycle
        last_seen_by_amount[charge["amount"]] = charge["date"]
    if cycle:
        return cycle

    if any(c["category"] == SUBSCRIPTION_CATEGORY for c in charges):
        for before, after in zip(charges, charges[1:]):
            near = abs(after["amount"] - before["amount"]) <= before["amount"] * NEAR_AMOUNT_RATE
            if near and _gap_cycle((after["date"] - before["date"]).days) == MONTHLY:
                return MONTHLY
    return None


def build_report(today: datetime) -> str:
    target_year, target_month = today.year, today.month
    prev_month_date = datetime(target_year, target_month, 1) - timedelta(days=1)
    prev_year, prev_month = prev_month_date.year, prev_month_date.month

    charges_by_merchant: dict[str, list[dict]] = {}
    for r in sorted(sheets_client.get_parsed_detail_rows(), key=lambda r: r["date"]):
        if r["amount"] > 0:  # 返金や0円の明細は、請求の間隔の判定に使わない
            charges_by_merchant.setdefault(_normalize(_base_name(r["merchant"])), []).append(r)

    subscriptions = []
    subscription_total = 0
    current_by_merchant: dict[str, int] = {}
    prev_by_merchant: dict[str, int] = {}

    for charges in charges_by_merchant.values():
        cycle = _subscription_cycle(charges)
        if not cycle:
            continue
        for r in charges:
            name = _base_name(r["merchant"])
            if r["date"].year == target_year and r["date"].month == target_month:
                subscriptions.append((r, cycle))
                subscription_total += r["amount"]
                current_by_merchant[name] = current_by_merchant.get(name, 0) + r["amount"]
            elif r["date"].year == prev_year and r["date"].month == prev_month:
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
        message += "今月、サブスクと確認できる請求はありませんでした。"
    else:
        for s, cycle in subscriptions:
            yearly = "年額" if cycle == YEARLY else f"年間換算 {s['amount'] * 12:,}円"
            message += f"・{s['merchant']}：{s['amount']:,}円（{yearly}）\n"
        message += "\n不要なものがあれば解約を検討してみてください。"
    message += "\n※同じ金額の請求が続いているものなどを載せています。1回だけの利用は載せていません。"

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
