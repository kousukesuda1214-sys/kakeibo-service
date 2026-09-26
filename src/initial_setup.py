"""
初回セットアップ用スクリプト。

空のGoogleスプレッドシートに、家計簿システムが必要とするシート
（取引明細・ログ・予算計画・送信元リスト・設定・カテゴリルール・カテゴリ予算）を、
正しいヘッダー・書式つきで自動的に作成する。

「日次利用額」「年間サマリー」「システム状態」は、自動実行のときに作られるので、
ここでは作らない。

■ 事前準備
1. 空のGoogleスプレッドシートを1つ作成し、そのURLからスプレッドシートID
   （ https://docs.google.com/spreadsheets/d/【ここの部分】/edit ）を控える
2. プロジェクトのルートに .env ファイルを作り、以下を書く
       GOOGLE_SHEET_ID=（↑で控えたID）
3. 配布者から受け取った gmail_client_secret.json を、同じフォルダ（src フォルダと
   同じ場所）に置く

■ 実行方法
    python -m src.initial_setup

初回はブラウザが開いて、自分のGoogleアカウントでの認証を求められる
（gmail_token.json が自動生成され、以降は不要になる）。
"""

import gspread

from . import config, sheets_client
from .gmail_client import get_credentials
from .settings import ensure_settings_sheet


def _client():
    return gspread.authorize(get_credentials())


def _open_spreadsheet():
    if not config.GOOGLE_SHEET_ID:
        raise SystemExit(
            "❌ .env に GOOGLE_SHEET_ID が設定されていません。\n"
            "   空のスプレッドシートを作成し、そのURLに含まれるIDを .env に書いてから、"
            "もう一度実行してください。"
        )
    return _client().open_by_key(config.GOOGLE_SHEET_ID)


def _ensure_sheet(ss, title: str, rows: int = 100, cols: int = 10):
    """指定した名前のシートが無ければ作成する。既にあればそのまま返す（中身は変更しない）。"""
    try:
        sheet = ss.worksheet(title)
        print(f"・「{title}」は既にあるので、そのままにします。")
        return sheet, False
    except gspread.exceptions.WorksheetNotFound:
        sheet = ss.add_worksheet(title=title, rows=rows, cols=cols)
        print(f"✅ 「{title}」を作成しました。")
        return sheet, True


def _apply_borders(sheet, start_row, end_row, start_col, end_col):
    if end_row < start_row:
        return
    border_style = {"style": "SOLID", "width": 1, "color": {"red": 0, "green": 0, "blue": 0}}
    sheet.spreadsheet.batch_update({
        "requests": [{
            "updateBorders": {
                "range": {
                    "sheetId": sheet.id,
                    "startRowIndex": start_row - 1,
                    "endRowIndex": end_row,
                    "startColumnIndex": start_col - 1,
                    "endColumnIndex": end_col,
                },
                "top": border_style, "bottom": border_style,
                "left": border_style, "right": border_style,
                "innerHorizontal": border_style, "innerVertical": border_style,
            }
        }]
    })


def setup_detail_sheet(ss):
    """「取引明細」：A列は空け、B2:E2にヘッダーを入れる。"""
    sheet, created = _ensure_sheet(ss, config.SHEET_DETAIL, rows=1000, cols=5)
    if created:
        sheet.update("B2:E2", [["日時", "店舗名", "金額", "カテゴリ"]], value_input_option="USER_ENTERED")
        sheet.format("B2:E2", {"textFormat": {"bold": True}})
        _apply_borders(sheet, start_row=2, end_row=2, start_col=2, end_col=5)


def setup_log_sheet(ss):
    """「ログ」：A列は空け、B2:C2にヘッダーを入れる。"""
    sheet, created = _ensure_sheet(ss, config.SHEET_LOG, rows=1000, cols=3)
    if created:
        sheet.update("B2:C2", [["日時", "金額"]], value_input_option="USER_ENTERED")
        sheet.format("B2:C2", {"textFormat": {"bold": True}})
        _apply_borders(sheet, start_row=2, end_row=2, start_col=2, end_col=3)


def setup_budget_sheet(ss):
    """「予算計画」：B2:I2にヘッダーを入れ、今月の行を1行追加しておく。
    （年月は「引落し月」の意味。必要な月の行は、自動実行のときに、この行の予算額を
    コピーして自動で追加されるので、今月の行が実際の引落し月と違っていても問題ない）"""
    sheet, created = _ensure_sheet(ss, config.SHEET_BUDGET, rows=100, cols=9)
    if created:
        headers = ["年月", "計画", "", "実績(月)", "実績(累計)", "差額(月)", "差額(累計)", "確定実績(締め)"]
        sheet.update("B2:I2", [headers], value_input_option="USER_ENTERED")
        sheet.format("B2:I2", {"textFormat": {"bold": True}})

        from datetime import datetime
        today = datetime.now()
        month_label = f"{today.year}年{today.month}月"
        sheet.update("B3:C3", [[month_label, config.SAMPLE_BUDGET_AMOUNT]], value_input_option="USER_ENTERED")
        print(f"   → 今月（{month_label}）の行を、仮の予算額（{config.SAMPLE_BUDGET_AMOUNT:,}円）で追加しました。"
              f"実際の金額に書き換えてください。")

        _apply_borders(sheet, start_row=2, end_row=3, start_col=2, end_col=9)


def setup_sender_list_sheet(ss):
    """「送信元リスト」：カード会社ごとの設定を入れる。config.SENDER_LISTの内容を初期値として書き込む。"""
    sheet, created = _ensure_sheet(ss, config.SHEET_SENDER_LIST, rows=20, cols=5)
    if created:
        headers = ["サービス名", "送信元メールアドレス", "除外キーワード（件名、カンマ区切り・任意）", "金額の目印文言（任意）"]
        sheet.update("B2:E2", [headers], value_input_option="USER_ENTERED")
        sheet.format("B2:E2", {"textFormat": {"bold": True}})

        rows = []
        service_names = ["三井住友カード(Vpass)", "Google Play"]
        for i, sender in enumerate(config.SENDER_LIST):
            name = service_names[i] if i < len(service_names) else sender["address"]
            excludes = ",".join(sender.get("exclude_subject_keywords") or [])
            amount_keyword = sender.get("amount_keyword") or ""
            rows.append([name, sender["address"], excludes, amount_keyword])

        if rows:
            sheet.update(f"B3:E{2 + len(rows)}", rows, value_input_option="USER_ENTERED")

        sample_row = 3 + len(rows)
        sheet.update(f"B{sample_row}:E{sample_row}", [[
            "（例）楽天カード", "info@mail.rakuten-card.co.jp", "", "ご利用金額"
        ]], value_input_option="USER_ENTERED")
        sheet.format(f"B{sample_row}:E{sample_row}", {
            "textFormat": {"foregroundColor": {"red": 0.6, "green": 0.6, "blue": 0.6}, "italic": True}
        })
        sheet.update(f"F{sample_row}", [[
            "← サンプル行です。実際のメールアドレス・文言に書き換えるか、削除してください"
        ]])

        print("   → お使いのカード会社に合わせて、行を追加・編集してください。")

        _apply_borders(sheet, start_row=2, end_row=sample_row, start_col=2, end_col=5)


def main() -> None:
    print("🔧 家計簿システムの初期セットアップを開始します。\n")
    ss = _open_spreadsheet()

    setup_detail_sheet(ss)
    setup_log_sheet(ss)
    setup_budget_sheet(ss)
    setup_sender_list_sheet(ss)

    # 以下の3つは、それぞれのモジュールの「無ければ作る」処理をそのまま使う
    ensure_settings_sheet()
    print(f"✅ 「設定」シートを用意しました。")
    sheets_client.ensure_category_rules_sheet()
    print(f"✅ 「{sheets_client.SHEET_CATEGORY_RULES}」シートを用意しました。")
    sheets_client.ensure_category_budget_sheet()
    print(f"✅ 「{config.SHEET_CATEGORY_BUDGET}」シートを用意しました。")

    print(
        "\n✅ 完了しました。\n\n"
        "次にやること：\n"
        "  1. 「設定」シートで、カードの締め日・引落し日・通知の時刻などを確認・変更する\n"
        "  2. 「送信元リスト」シートを開き、お使いのカード会社の情報を確認・編集する\n"
        "  3. 「予算計画」シートの今月の行に、実際の予算額を入力する\n"
        "  4. gmail_token.json の中身などを、GitHub Secrets に登録する\n"
        "  5. （過去のメールも取り込みたい場合）python -m src.rebuild_all_transactions を実行する"
    )


if __name__ == "__main__":
    main()