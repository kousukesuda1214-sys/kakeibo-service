"""
ヘルスチェック：今月分のカード利用通知メールがどれくらい正常に処理できたかを、
月末にLINEで報告する。GAS版のsendHealthCheckを移植。
送信元は「送信元リスト」シートを最優先で読む（main.pyと同じ扱い）。

■ 雛形化での変更点
Gmailの before: は「その日を含まない」ため、旧版では月末当日のメールが集計から
漏れていた。翌日を before: に指定するよう修正した。
"""

from datetime import datetime, timedelta

from . import sheets_client, line_client, parser
from .gmail_client import get_gmail_service, _build_query, _get_plain_body  # noqa: F401 (内部関数を流用)
from .periods import in_report_window, is_last_day_of_month


def build_report(today: datetime) -> str:
    period_start = datetime(today.year, today.month, 1)
    after_str = period_start.strftime("%Y/%m/%d")
    before_str = (today + timedelta(days=1)).strftime("%Y/%m/%d")

    service = get_gmail_service()
    sender_configs = sheets_client.get_sender_configs()
    # ラベルによる絞り込みを外し、今月分の対象メール全体を数える
    base_query = _build_query(sender_configs).split(" -label:")[0]
    query = f"{base_query} after:{after_str} before:{before_str}"

    total_emails = 0
    matched_emails = 0
    page_token = None
    while True:
        resp = service.users().messages().list(userId="me", q=query, pageToken=page_token).execute()
        for msg_meta in resp.get("messages", []):
            msg = service.users().messages().get(userId="me", id=msg_meta["id"], format="full").execute()
            body = _get_plain_body(msg["payload"])
            total_emails += 1
            if parser.extract_amount_from_message(body, None):
                matched_emails += 1
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    unmatched = total_emails - matched_emails
    message = (
        f"✅ システムヘルスチェック（{period_start.month}月{period_start.day}日〜{today.month}月{today.day}日）\n"
        f"処理対象メール：{total_emails}件\n"
        f"正常に金額を抽出：{matched_emails}件\n"
    )
    message += (
        f"⚠️ 抽出できなかったメール：{unmatched}件"
        "（「送信元リスト」シートの「金額の目印文言」が合っているか確認してください）"
        if unmatched > 0 else
        "問題なく全件処理できています。"
    )
    return message


def maybe_send_health_check(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    if not is_last_day_of_month(now):
        return False
    if not in_report_window(now):
        return False
    if sheets_client.get_state("health_check") == now.strftime("%Y/%m/%d"):
        return False

    message = build_report(now)
    line_client.send_line_message(message)
    sheets_client.set_state("health_check", now.strftime("%Y/%m/%d"))
    print("ヘルスチェックを送信しました")
    return True
