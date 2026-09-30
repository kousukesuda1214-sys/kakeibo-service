"""
取引明細・ログの全期間再構築スクリプト（過去のメールをまとめて取り込む）

■ こんなときに使う
- 使い始めたとき：Gmailに残っている過去のカード利用通知メールを、まとめて取り込む
  （これを先にやっておくと、自動実行の初回に過去メールを1件ずつ処理して時間切れに
  なるのを防げる。処理済みラベルもまとめて付くので、以後は新着だけが処理される）
- 「送信元リスト」にカード会社を追加したとき：そのカードの過去分も取り込む
- 「カテゴリルール」を大きく変えたとき：全取引をカテゴリ判定し直す

■ 安全のための順序
  ① Gmail全期間を読み切って、メモリ上に全件貯める（この間はシートに一切触れない）
  ② 抽出件数を表示し、実行するか確認を取る
  ③ 「取引明細」「ログ」の今の中身を、別シートに複製してバックアップする
  ④ そこで初めてシートを空にし、貯めておいたデータを一括で書き込む
  ⑤ すべて成功した後にだけ、Gmail側に「処理済み」ラベルを付ける
途中で失敗しても③まではシートを一切変更しておらず、④以降で失敗した場合も
③で作ったバックアップシートから手動で復元できる。

■ 家計簿サービス（kakeibo-service）では使わない
  サービス版の利用者の家計簿は、運営者のパソコンからは開けない（権限が利用者ごとの一時的な鍵だけのため）。
  サービス版で過去のメールを取り込み直したいときは、連携し直しの仕組み（user_run.prepare_reimport）が自動で行う。
  このスクリプトは、自分のGoogleアカウントの家計簿（晃介さん専用版と同じ使い方）に対してだけ使う。

■ 実行方法（自分のパソコンで実行する。GitHub Actionsでは実行できない）
    python -m src.rebuild_all_transactions

■ 実行後
バックアップシートは確認が終わったら手動で削除してよい。
「予算計画」の過去の月の実績（E列）は自動では計算し直されない（進行中の月は、
次回の日次決算のタイミングで正しい値に更新される）。
"""

import random
import socket
import ssl
import time
from datetime import datetime
from http.client import IncompleteRead

from googleapiclient.errors import HttpError

from . import config, parser, sheets_client
from .gmail_client import _build_query, _get_or_create_label_id, _get_plain_body, get_gmail_service
from .main import SKIP_SUBJECT, parse_card_email
from .settings import apply_sheet_settings


# Gmail APIは「クォータ超過」を429だけでなく403（reason: rateLimitExceeded /
# userRateLimitExceeded / quotaExceeded）で返してくることがある。ステータスコードだけで
# 判定すると見落とすため、エラー本文の中身も見て判定する。
_RATE_LIMIT_REASONS = ("rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "backendError")

# 2026/09/15 追加：Gmail APIが明示的なエラーレスポンスを返す場合（HttpError）だけでなく、
# 通信そのものが一時的にタイムアウトする場合（TimeoutError・ssl.SSLError・接続断など）にも
# 遭遇したため、これらもまとめてリトライ対象にする。
_RETRYABLE_NETWORK_ERRORS = (TimeoutError, socket.timeout, ssl.SSLError, ConnectionError, IncompleteRead)


def _is_rate_limit_error(e: HttpError) -> bool:
    status = getattr(e.resp, "status", None)
    if status in (429, 500, 503):
        return True
    if status == 403:
        content = e.content.decode("utf-8", errors="ignore") if isinstance(e.content, bytes) else str(e.content)
        return any(reason in content for reason in _RATE_LIMIT_REASONS)
    return False


def _call_with_retry(func, *args, max_retries: int = 8, **kwargs):
    """Gmail APIの呼び出しを、レート制限・クォータ超過・一時的なサーバーエラーだけでなく、
    通信そのものの一時的なタイムアウト・切断（TimeoutError等）に対しても
    自動でリトライする（指数バックオフ＋ランダムなゆらぎ、最大60秒待ち）。"""
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except HttpError as e:
            if _is_rate_limit_error(e) and attempt < max_retries - 1:
                wait = min((2 ** attempt) + random.uniform(0, 1), 60)
                print(f"  ⚠️ APIのクォータ制限に達しました。{wait:.1f}秒待って再試行します…（{attempt + 1}/{max_retries}回目）")
                time.sleep(wait)
                continue
            raise
        except _RETRYABLE_NETWORK_ERRORS as e:
            if attempt < max_retries - 1:
                wait = min((2 ** attempt) + random.uniform(0, 1), 60)
                print(f"  ⚠️ 通信が一時的にタイムアウトしました（{type(e).__name__}）。{wait:.1f}秒待って再試行します…（{attempt + 1}/{max_retries}回目）")
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("リトライ上限に達しました")


def _fetch_all_message_ids(service, query: str) -> list[str]:
    """条件に一致する全メッセージIDを、ページネーションしながら集める。
    同じメッセージが複数ページにまたがって重複して返ってくることが稀にあるため
    （実行中にメールボックスの状態が変わるなど）、setで重複を除いてから返す
    （同じ買い物が為替レート違いで2重に記録される事故の一因になっていたため追加した）。"""
    ids: list[str] = []
    seen: set[str] = set()
    page_token = None
    while True:
        resp = _call_with_retry(
            service.users().messages().list(userId="me", q=query, pageToken=page_token).execute
        )
        for m in resp.get("messages", []):
            if m["id"] not in seen:
                seen.add(m["id"])
                ids.append(m["id"])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def main() -> None:
    print("=" * 60)
    print("Gmail全期間から「取引明細」「ログ」を安全に作り直します")
    print("=" * 60)

    # タイムゾーン・締め時刻などを「設定」シートに合わせる（記録日の判定に影響するため）
    apply_sheet_settings()

    service = get_gmail_service()

    # 「送信元リスト」シートを最優先で読む（GASのgetSenderConfigsに相当）
    sender_configs = sheets_client.get_sender_configs()

    # 処理済みラベルによる除外を外し、全期間を対象にする
    query = _build_query(sender_configs).split(" -label:")[0]

    print("\n① 対象メールを数えています…")
    all_message_ids = _fetch_all_message_ids(service, query)
    print(f"対象メール件数：{len(all_message_ids)}件")

    if not all_message_ids:
        print("対象メールが見つかりませんでした。処理を終了します。")
        return

    print("\n② メール本文を読み込んで、金額・店舗名を抽出しています…")
    print("　（この段階ではスプレッドシートには一切書き込みません）")

    extracted_rows: list[tuple] = []  # [(effective_date, merchant, amount, category), ...]
    date_totals: dict[str, int] = {}
    message_ids_to_label: list[str] = []

    for idx, msg_id in enumerate(all_message_ids, start=1):
        msg = _call_with_retry(
            service.users().messages().get(userId="me", id=msg_id, format="full").execute
        )
        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
        body = _get_plain_body(msg["payload"])
        subject = headers.get("Subject", "")
        from_header = headers.get("From", "")

        parsed = parse_card_email(subject, from_header, body, int(msg["internalDate"]), sender_configs)
        if parsed == SKIP_SUBJECT:
            # 読み取る件名に合わないメール（宣伝メールなど）は、記録もラベル付けもしない
            continue
        if parsed:
            effective_date, merchant, amount, category = parsed
            extracted_rows.append((effective_date, merchant, amount, category))

            date_key = effective_date.strftime("%Y/%m/%d")
            date_totals[date_key] = date_totals.get(date_key, 0) + amount

        message_ids_to_label.append(msg_id)

        # クォータ超過を未然に防ぐため、1件ごとにごく短い間隔を空ける
        time.sleep(0.3)

        if idx % 20 == 0 or idx == len(all_message_ids):
            print(f"  {idx}/{len(all_message_ids)}件 読み込み済み…")

    print(f"\n読み込み完了：{len(extracted_rows)}件の取引を抽出（対象日数：{len(date_totals)}日分）")
    print("⚠️ 「取引明細」「ログ」の現在の中身は、書き込み前にバックアップシートとして複製します。")
    answer = input("この内容で「取引明細」「ログ」を作り直しますか？ (y/N): ").strip().lower()
    if answer != "y":
        print("キャンセルしました。スプレッドシート・Gmailともに一切変更していません。")
        return

    print("\n③ 現在のシートをバックアップしています…")
    detail_backup = sheets_client.backup_sheet(config.SHEET_DETAIL)
    log_backup = sheets_client.backup_sheet(config.SHEET_LOG)
    print(f"　バックアップ作成：「{detail_backup}」「{log_backup}」")

    print("\n④ シートを空にして、抽出したデータを一括で書き込んでいます…")
    sheets_client.clear_detail_and_log()
    sheets_client.bulk_append_transactions(extracted_rows)
    sheets_client.bulk_write_log(date_totals)

    print("\n⑤ 完全一致の重複を整理しています…")
    removed_count, affected_dates = sheets_client.cleanup_duplicate_transactions()

    print("⑤-2 即時通知/確定通知の二重記録を整理しています…")
    reconciled_count, reconciled_dates = sheets_client.reconcile_quick_and_confirmed_transactions()

    print("⑤-2b 楽天カードの速報版と店名入りの通知の二重記録を整理しています…")
    flash_count, _flash_dates = sheets_client.reconcile_flash_reports()
    if flash_count:
        print(f"　速報版を{flash_count}件、店名入りの記録にまとめました")

    print("⑤-3 同一日時・同一店舗の為替レート違いによる重複を整理しています…")
    same_moment_count, same_moment_dates = sheets_client.cleanup_same_moment_duplicates()

    print("⑥ 日時で並べ替え、枠線を更新しています…")
    sheets_client.sort_detail_and_log()
    sheets_client.apply_detail_sheet_borders()
    sheets_client.apply_log_sheet_borders()

    print("⑦ 「日次利用額」シートを作り直しています…")
    sheets_client.rebuild_daily_usage_sheet()

    print("\n⑧ Gmail側に処理済みラベルを付けています…")
    label_id = _get_or_create_label_id(service)
    for idx, msg_id in enumerate(message_ids_to_label, start=1):
        _call_with_retry(
            service.users().messages().modify(
                userId="me", id=msg_id, body={"addLabelIds": [label_id]}
            ).execute
        )
        time.sleep(0.2)

        if idx % 50 == 0 or idx == len(message_ids_to_label):
            print(f"  {idx}/{len(message_ids_to_label)}件 ラベル付与済み…")

    print("\n" + "=" * 60)
    print(f"✅ 完了しました。取引：{len(extracted_rows)}件、対象日数：{len(date_totals)}日分")
    if removed_count > 0:
        print(f"　（うち、完全一致の重複を{removed_count}件、自動で整理しました：{', '.join(affected_dates)}）")
    if reconciled_count > 0:
        print(f"　（うち、即時通知/確定通知の二重記録を{reconciled_count}件、確定額に統合しました：{', '.join(reconciled_dates)}）")
    if same_moment_count > 0:
        print(f"　（うち、同一日時・同一店舗の為替レート違いによる重複を{same_moment_count}件、整理しました：{', '.join(same_moment_dates)}）")
    print(f"\nバックアップシート（確認が終わったら手動で削除してください）：")
    print(f"　「{detail_backup}」「{log_backup}」")
    print("=" * 60)


if __name__ == "__main__":
    main()