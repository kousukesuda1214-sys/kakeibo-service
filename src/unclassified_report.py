"""
未分類店舗レポート：「その他」に分類されている店舗名を、数日おきにLINEへ送る。
ここに出てきた店舗名のキーワードを「カテゴリルール」シートに追加すれば、以後は自動で
分類される。ANTHROPIC_API_KEY未設定時（AIによる自動分類が無いとき）にだけmain.pyから
呼ばれる。GAS版のsendUnclassifiedMerchantsReportを移植。INTERVAL_DAYS日ごとに送る。
"""

from datetime import datetime

from . import sheets_client, line_client
from .periods import in_report_window

INTERVAL_DAYS = 3  # 何日ごとに送るか


def build_report() -> str:
    entries = sheets_client.get_unclassified_merchants_summary()

    if not entries:
        return "📂 未分類店舗レポート\n\n「その他」に分類されている店舗はありません。"

    message = (
        f"📂 未分類店舗レポート（{len(entries)}種類）\n"
        "スプレッドシートの「カテゴリルール」シートに、この店舗名の一部をキーワードとして"
        "追加すると、次からは自動で分類されます。\n\n"
    )
    for name, info in entries[:20]:
        message += f"・{name}　{info['count']}件　計¥{info['total']:,}\n"
    if len(entries) > 20:
        message += f"\n...他{len(entries) - 20}種類（スプレッドシートで確認してください）"

    return message.strip()


def maybe_send_unclassified_report(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    if not in_report_window(now):
        return False

    last_sent = sheets_client.get_state("unclassified_report")
    today_str = now.strftime("%Y/%m/%d")
    if last_sent == today_str:
        return False
    if last_sent:
        last_date = datetime.strptime(last_sent, "%Y/%m/%d")
        if (now - last_date).days < INTERVAL_DAYS:
            return False

    message = build_report()
    line_client.send_line_message(message)
    sheets_client.set_state("unclassified_report", today_str)
    print("未分類店舗レポートを送信しました")
    return True
