"""
家計簿システムのエントリーポイント。
「Gmail解析 → 取引明細への記録 → 高額利用の即時LINEアラート」というコアフローに加えて、
日次・週次決算、月次振り返りコメント、サブスク棚卸し、ヘルスチェック、年間サマリー、
未分類店舗レポート、重複取引の自動整理・警告、日次利用額シートの再構築、
支払日通知（三井住友カード利用者向け・任意）までをすべて実行する。

■ 実行方法
    python -m src.main

■ 定期実行（GitHub Actionsで15分おき）
    .github/workflows/daily.yml を参照

■ 雛形化での変更点
- 締め日・通知時刻・タイムゾーンなどの設定を、スプレッドシートの「設定」シートから
  読み込むようにした（settings.py）。
- 「その日の締め時刻」（これ以降に届いた通知は翌日扱い）を、日次決算の送信時刻
  （DAILY_REPORT_HOUR）の30分後に合わせた（旧：19:30固定）。
- 高額利用アラートは、利用日が直近 HIGH_AMOUNT_ALERT_MAX_AGE_DAYS 日以内のものだけに
  送るようにした。初めて使い始めた日に、過去の取引をまとめて処理したときに
  昔の高額利用のアラートが大量に届いてしまうのを防ぐため。
- ANTHROPIC_API_KEY が未設定の場合（＝AIによるカテゴリ自動判定が使えない場合）は、
  「その他」に分類された店舗を数日おきに知らせる未分類店舗レポートを送る。

■ 堅牢性について（kakeibo-python時代の経緯）
- 本文に「ご利用日時：YYYY/MM/DD」があれば、メールの受信日時よりそちらを優先して
  記録日にする（通知メールが実際の利用日から数日遅れて届くことがあるため）。
- Gmail APIの取得処理そのものがリトライを尽くしても失敗した場合でも、そこまでの分は
  確定させ、後続の重複整理・決算処理は必ず実行する。
"""

from datetime import datetime, timedelta

from . import (
    config,
    parser,
    line_client,
    sheets_client,
    daily_report,
    notify,
    closing_notice,
    weekly_report,
    subscription_report,
    health_check,
    annual_summary,
    review_comment,
    unclassified_report,
)
from .gmail_client import get_gmail_service, fetch_unprocessed_messages
from .settings import apply_sheet_settings

# この時刻以降に受信した（本文に利用日時の記載が無い）通知は、翌日の取引として扱う。
# 日次決算を送った後の利用が「今日」に紛れ込まないよう、送信時刻（DAILY_REPORT_HOUR）の
# 30分後にしている。DAILY_REPORT_HOURは「設定」シートで変わるため、使う時点で読む。
CUTOFF_MINUTE = 30

# 高額利用アラートを送る対象を、利用日が直近この日数以内のものに限る
HIGH_AMOUNT_ALERT_MAX_AGE_DAYS = 3


def get_effective_date(raw_date: datetime, body: str = "") -> datetime:
    """
    取引の「記録日」を決定する（GASのgetEffectiveDateを移植・拡張）。

    優先順位：
    ① 本文に「ご利用日時：YYYY/MM/DD」の記載があれば、そちらを使う
       （カード会社の通知タイミングのズレに影響されないようにするため）
    ② 本文から読み取れない場合は、従来通りメール受信時刻を使い、
       締め時刻（DAILY_REPORT_HOUR の30分後）以降に受信したものは翌日扱いにする
    """
    usage_date = parser.extract_usage_date(body)
    if usage_date:
        return usage_date

    is_after_cutoff = (raw_date.hour, raw_date.minute) >= (config.DAILY_REPORT_HOUR, CUTOFF_MINUTE)
    if is_after_cutoff:
        return raw_date + timedelta(days=1)
    return raw_date


def find_sender_config(from_header: str, sender_configs: list[dict]) -> dict | None:
    for sender in sender_configs:
        if sender["address"] in from_header:
            return sender
    return None


def process_new_emails() -> int:
    """
    新着のカード利用通知メールを処理する。処理件数を返す。

    fetch_unprocessed_messages()はジェネレーターで、その内部（Gmail APIの
    list/get/modify呼び出し）がリトライを尽くしても失敗することがある。
    for文でそのまま回すと例外がrun()全体を止めてしまうため、next()を手動で呼び、
    失敗したら「今回はここまで」として打ち切る（それまでの分は確定させ、
    run()の後続処理は必ず実行する）。未処理のまま残ったメールは、ラベルが
    付いていないので次回の実行で自然に拾われる。
    """
    service = get_gmail_service()
    matched_count = 0

    # 「送信元リスト」シートを最優先で読む（GASのgetSenderConfigsに相当。
    # シートが無い・空ならconfig.SENDER_LISTにフォールバックする）
    sender_configs = sheets_client.get_sender_configs()

    message_iter = fetch_unprocessed_messages(service, sender_configs)
    while True:
        try:
            msg = next(message_iter)
        except StopIteration:
            break
        except Exception as e:
            print(f"新着メールの取得中にエラーが発生したため、今回はここで打ち切ります: {e}")
            notify.notify_error("process_new_emails（fetch_unprocessed_messages）", e)
            break

        try:
            sender_config = find_sender_config(msg["from"], sender_configs)
            amount_keyword = sender_config.get("amount_keyword") if sender_config else None

            extracted = parser.extract_amount_from_message(msg["body"], amount_keyword)
            if not extracted:
                print(f"金額を抽出できませんでした: {msg['subject']}")
                continue

            matched_count += 1

            merchant = parser.extract_merchant_name(msg["body"], extracted.match_index, msg["subject"])
            if extracted.note:
                merchant = f"{merchant}（{extracted.note}）"

            amount = extracted.amount
            if parser.is_refund(msg["subject"], msg["body"]):
                amount = -abs(amount)
                merchant = f"{merchant}（返金）"

            category = parser.categorize(merchant)
            if category == config.DEFAULT_CATEGORY:
                category = parser.ai_categorize(merchant)
            raw_date = datetime.fromtimestamp(msg["date_ms"] / 1000)
            effective_date = get_effective_date(raw_date, msg["body"])

            sheets_client.append_transaction(effective_date, merchant, amount, category)
            sheets_client.add_to_log(effective_date.strftime("%Y/%m/%d"), amount)

            print(f"記録: {effective_date:%Y/%m/%d %H:%M} {merchant} ¥{amount:,} [{category}]")

            # 高額利用の即時アラート（返金は対象外。過去分をまとめて処理したときに
            # 昔の取引のアラートが大量に届かないよう、直近の利用だけを対象にする）
            is_recent = datetime.now() - effective_date <= timedelta(days=HIGH_AMOUNT_ALERT_MAX_AGE_DAYS)
            if amount >= config.HIGH_AMOUNT_THRESHOLD and is_recent:
                line_client.send_line_message(
                    "🚨 高額利用アラート\n\n"
                    f"{merchant}\n"
                    f"¥{amount:,}\n"
                    f"{effective_date:%m月%d日 %H:%M}\n\n"
                    "身に覚えのない利用の場合は、すぐにカード会社に確認してください。"
                )
        except Exception as e:
            # 1件の処理に失敗しても、他のメールの処理は続ける。
            # ラベルはすでに付与済みなので再処理されないが、記録漏れに気づけるようメール通知する。
            print(f"メールの処理に失敗しました（件名: {msg['subject']}）: {e}")
            notify.notify_error(f"process_new_emails（件名: {msg['subject']}）", e)

    return matched_count


def run() -> None:
    # 「設定」シートの値（締め日・通知時刻・タイムゾーンなど）を最初に反映する
    apply_sheet_settings()

    count = process_new_emails()
    print(f"未処理メール {count} 件を処理しました")

    # ①重複の完全一致は自動整理し、結果をLINEで報告する
    removed_count, affected_dates = sheets_client.cleanup_duplicate_transactions()
    if removed_count > 0:
        print(f"完全一致の重複を{removed_count}件、自動で整理しました（対象日: {', '.join(affected_dates)}）")
        line_client.send_line_message(
            f"🧹 完全一致の重複取引を{removed_count}件、自動で整理しました。\n\n"
            f"対象日：{', '.join(affected_dates)}\n\n"
            "「取引明細」「ログ」ともに正しい状態に更新済みです。"
        )

    # ①-2 三井住友カード(Vpass)の即時通知（外貨建て推定）と確定通知（円建て確定額）が同じ買い物を
    # 指している二重記録は、確定額の方を残して1件にまとめる
    reconciled_count, reconciled_dates = sheets_client.reconcile_quick_and_confirmed_transactions()
    if reconciled_count > 0:
        print(f"即時通知/確定通知の二重記録を{reconciled_count}件、確定額に統合しました（対象日: {', '.join(reconciled_dates)}）")
        line_client.send_line_message(
            f"🔁 同じ買い物が即時通知（推定レート）と確定通知（確定額）で二重に記録されていた"
            f"{reconciled_count}件を、確定額に統合しました。\n\n"
            f"対象日：{', '.join(reconciled_dates)}"
        )

    # ①-3 同じ日時・同じ店舗が、為替レートの違いだけで金額違いの別行になっている
    # ものも整理する（全期間再構築を日をまたいで複数回行った際に起きる）
    same_moment_count, same_moment_dates = sheets_client.cleanup_same_moment_duplicates()
    if same_moment_count > 0:
        print(f"同一日時・同一店舗の為替レート違いによる重複を{same_moment_count}件、整理しました（対象日: {', '.join(same_moment_dates)}）")

    # ②「取引明細」「ログ」を日時で並べ替え、「日次利用額」シートを取引明細から作り直す
    # （毎回。処理は軽いので常に実行）
    sheets_client.sort_detail_and_log()
    sheets_client.apply_detail_sheet_borders()
    sheets_client.apply_log_sheet_borders()
    sheets_client.rebuild_daily_usage_sheet()

    # ③似ているだけで完全一致ではない「近い重複」は、削除せず警告のみ行う（新着があった時だけ）
    if count > 0:
        warnings = sheets_client.find_near_duplicate_warnings()
        if warnings:
            message = f"⚠️ 重複の疑いがある取引を{len(warnings)}件見つけました\n\n"
            message += "\n".join(warnings[:10])
            if len(warnings) > 10:
                message += f"\n...他{len(warnings) - 10}件"
            message += "\n\n「取引明細」で内容を確認してください。"
            line_client.send_line_message(message)

    # ④日次・週次決算
    daily_report.maybe_send_daily_report()
    weekly_report.maybe_send_weekly_report()

    # ⑤締め日の月次振り返りコメント、月末のサブスク棚卸し・ヘルスチェック、年末の年間サマリー
    review_comment.maybe_send_review_comment()
    subscription_report.maybe_send_subscription_list()
    health_check.maybe_send_health_check()
    annual_summary.maybe_build_annual_summary()

    # ⑥未分類店舗レポート：ANTHROPIC_API_KEYがあれば、キーワードで判定できない店舗は
    # ai_categorize()が自動で分類するので不要。無い場合は「その他」が溜まっていくので、
    # 数日おきに知らせて「カテゴリルール」シートへの追加を促す。
    if not config.ANTHROPIC_API_KEY:
        unclassified_report.maybe_send_unclassified_report()

    # ⑦支払日通知メールの検知（三井住友カード利用者向け。ENABLE_PAYMENT_NOTICE=false なら何もしない）
    closing_notice.process_payment_notice_email()


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        notify.notify_error("main", e)
        raise