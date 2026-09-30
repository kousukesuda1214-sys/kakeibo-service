"""
家計簿サービス：利用者1人分の処理。

runner.py（親玉）から、利用者ごとに「別々のプロセス」として呼ばれる。
利用者ごとの設定（スプレッドシートのID・LINEの送り先・Googleの一時的な鍵・タイムゾーンなど）は、
すべて環境変数で渡される。別プロセスにしているのは、ある利用者の設定やキャッシュが、
次の利用者の処理に混ざらないようにするため。

■ 環境変数（runner.py が設定する）
  GOOGLE_ACCESS_TOKEN … 中継役から受け取った、その人の一時的な鍵（1時間で使えなくなる）
  GOOGLE_SHEET_ID     … その人の家計簿スプレッドシート
  LINE_USER_ID        … その人のLINEのユーザーID
  SHEET_READY         … 家計簿のシートの準備が済んでいれば "1"
  PROCESS_AFTER       … これより前のメールは処理しない（yyyy/mm/dd）
  MAX_MESSAGES_PER_RUN … 1回の実行で読むメールの上限（残りは次回）
  KAKEIBO_SERVICE_MODE … "1"（エラーを利用者ではなく親玉に伝えるための目印）
"""

from . import config, gmail_client

# 晃介さん専用版（kakeibo-python）と同じGmailを読む場合でも、お互いの処理済みの目印が
# 混ざらないよう、サービス版は別の名前のラベルを使う
gmail_client.PROCESSED_LABEL_NAME = "家計簿サービス_処理済み"
config.PAYMENT_NOTICE_PROCESSED_LABEL = "家計簿サービス_支払日通知_処理済み"

import base64  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402

from datetime import datetime, timedelta  # noqa: E402

from . import initial_setup, line_client, notify, sheets_client  # noqa: E402
from . import main as kakeibo_main  # noqa: E402
from .settings import ensure_settings_sheet  # noqa: E402
from .sheet_style import apply_layout_if_outdated, ensure_sheet_order  # noqa: E402
from . import snapshot  # noqa: E402

DEFAULT_SHEET_TITLES = ("シート1", "Sheet1")


def _is_long_import() -> bool:
    """取り込む範囲が1か月半より長い（＝以前にも使っていた人の取り込み直し）かどうか。"""
    try:
        after = datetime.strptime(os.environ.get("PROCESS_AFTER", ""), "%Y/%m/%d")
    except ValueError:
        return False
    return datetime.now() - after > timedelta(days=45)


def prepare_reimport() -> int:
    """新しい家計簿がまだ空のときに、以前の家計簿で処理済みになっていたメールの目印を外す。
    同じGoogleアカウントで連携し直したが、前の家計簿を開けずに新しく作った場合に、
    過去のカード利用を新しい家計簿へ取り込み直すため（目印が付いたままだと読まれない）。
    家計簿に1件でも記録があれば何もしないので、何度呼ばれても二重には記録されない。
    目印を外したメールの件数を返す（初めての利用者は0件）。"""
    if sheets_client.get_parsed_detail_rows():
        return 0
    service = gmail_client.get_gmail_service()
    ids = gmail_client.find_processed_message_ids(service, sheets_client.get_sender_configs())
    gmail_client.remove_processed_label(service, ids)
    return len(ids)


def setup_sheet() -> None:
    """新しい利用者の家計簿スプレッドシートに、必要なシートを作る（中継役が作った空のスプレッドシートに対して）。"""
    ss = sheets_client._spreadsheet()
    ensure_settings_sheet()
    initial_setup.setup_sender_list_sheet(ss)
    initial_setup.setup_budget_sheet(ss)
    initial_setup.setup_detail_sheet(ss)
    initial_setup.setup_log_sheet(ss)
    sheets_client.ensure_category_rules_sheet()
    sheets_client.ensure_category_budget_sheet()

    # 最初からある空の「シート1」は不要なので消す
    for ws in ss.worksheets():
        if ws.title in DEFAULT_SHEET_TITLES and len(ss.worksheets()) > 1:
            ss.del_worksheet(ws)

    # 以前の家計簿で処理済みのメールがあれば、取り込み直せるように目印を外す
    reimport_count = prepare_reimport()
    if reimport_count:
        print(f"以前に処理したメール{reimport_count}件を、新しい家計簿に取り込み直します")

    print("KAKEIBO_SHEET_READY")  # runner.py が、中継役に「準備済み」と伝えるための目印

    sheet_url = f"https://docs.google.com/spreadsheets/d/{config.GOOGLE_SHEET_ID}/edit"
    line_client.send_line_message(
        "🎉 家計簿の準備ができました！\n\n"
        f"あなたの家計簿：\n{sheet_url}\n\n"
        "【最初にやること】\n"
        "①「送信元リスト」シートに、お使いのカード会社の通知メールのアドレスがあるか確認する（無ければ追加）\n"
        "②「設定」シートで、カードの締め日・引落し日を確認する\n"
        "③「予算計画」シートに、毎月の予算を入力する\n\n"
        + (
            "これまでのカード利用メールも、このあと自動で取り込み直します（量が多いと数時間かかります）。\n"
            if reimport_count or _is_long_import()
            else "直近1か月分のカード利用メールも、このあと自動で取り込みます。\n"
        )
        + "毎日決まった時刻に、このLINEで日次決算をお知らせします。"
    )


def run() -> None:
    if os.environ.get("SHEET_READY") != "1":
        setup_sheet()
    kakeibo_main.run()

    # 見た目（列の幅・3桁区切りなど）が最新の版でなければ反映する。
    # 「日次利用額」などは kakeibo_main.run() の中で作られるので、最後に行う。
    # 見た目の反映に失敗しても、家計簿の記録そのものには影響しないので、止めずに報告だけする
    try:
        apply_layout_if_outdated()
        ensure_sheet_order()
    except Exception as e:
        notify.notify_error("apply_layout", e)

    # LINEのメニュー（「今日の決算」など）で見るための最新の数字を作り、runner.py に渡す。
    # 実行ログには出さない（runner.py がこの行を受け取って中継役に預け、表示はしない）
    try:
        data = json.dumps(snapshot.build(), ensure_ascii=False).encode("utf-8")
        print("KAKEIBO_SNAPSHOT|" + base64.b64encode(data).decode("ascii"))
    except Exception as e:
        notify.notify_error("snapshot", e)


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        notify.notify_error("user_run", e)
        raise SystemExit(1)
