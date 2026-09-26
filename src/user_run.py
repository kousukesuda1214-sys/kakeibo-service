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
  KAKEIBO_SERVICE_MODE … "1"（エラーを利用者ではなく親玉に伝えるための目印）
"""

from . import config, gmail_client

# 晃介さん専用版（kakeibo-python）と同じGmailを読む場合でも、お互いの処理済みの目印が
# 混ざらないよう、サービス版は別の名前のラベルを使う
gmail_client.PROCESSED_LABEL_NAME = "家計簿サービス_処理済み"
config.PAYMENT_NOTICE_PROCESSED_LABEL = "家計簿サービス_支払日通知_処理済み"

import os  # noqa: E402

from . import initial_setup, line_client, notify, sheets_client  # noqa: E402
from . import main as kakeibo_main  # noqa: E402
from .settings import ensure_settings_sheet  # noqa: E402

DEFAULT_SHEET_TITLES = ("シート1", "Sheet1")


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

    print("KAKEIBO_SHEET_READY")  # runner.py が、中継役に「準備済み」と伝えるための目印

    sheet_url = f"https://docs.google.com/spreadsheets/d/{config.GOOGLE_SHEET_ID}/edit"
    line_client.send_line_message(
        "🎉 家計簿の準備ができました！\n\n"
        f"あなたの家計簿：\n{sheet_url}\n\n"
        "【最初にやること】\n"
        "①「送信元リスト」シートに、お使いのカード会社の通知メールのアドレスがあるか確認する（無ければ追加）\n"
        "②「設定」シートで、カードの締め日・引落し日を確認する\n"
        "③「予算計画」シートに、毎月の予算を入力する\n\n"
        "直近1か月分のカード利用メールも、このあと自動で取り込みます。\n"
        "毎日決まった時刻に、このLINEで日次決算をお知らせします。"
    )


def run() -> None:
    if os.environ.get("SHEET_READY") != "1":
        setup_sheet()
    kakeibo_main.run()


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        notify.notify_error("user_run", e)
        raise SystemExit(1)
