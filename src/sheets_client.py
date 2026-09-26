"""
Googleスプレッドシートへの読み書き。

■ セットアップ
サービスアカウントは使わず、gmail_client.pyと同じOAuth認証（自分のGoogleアカウントの
ログイン許可）を使い回す設計にしている。追加のファイルは不要で、.env（または
GitHub Secrets）にGOOGLE_SHEET_ID（スプレッドシートのURLに含まれるID）だけ設定すればよい。
シートの初期作成は python -m src.initial_setup で行う。

■ 雛形化での変更点
- カテゴリ判定のキーワードを「カテゴリルール」シートから読むようにした
  （get_category_rules）。config.pyを直接編集すると本家テンプレートの自動アップデートと
  衝突してしまうため。シートが無ければ、初回にconfig.CATEGORY_RULESの内容で自動作成する。
- 「カテゴリ予算」「年間サマリー」のカテゴリ一覧も、このシートの内容に合わせるようにした。
- 「予算計画」の累計列（F・H列）の再計算を、1セルずつの書き込みから一括書き込みに変更
  （月数が増えるとGoogle Sheets APIの書き込み回数制限に引っかかるため）。

■ 書き込み方法について（kakeibo-python時代の経緯）
append_row()/append_rows() は、A列を空けたまま使うこのシート構成だとGoogleの
「テーブル自動検出」が誤動作し、ヘッダー行を上書きしてしまうことがあったため使わない。
行数を明示的に数えてから、該当行に直接updateで書き込む。
また、一時的な通信エラー（Wi-Fi瞬断・SSLエラー・Google側の5xx等）は
_call_with_retry() で間隔を空けて自動的に数回までリトライする。
"""

import json
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta

import gspread
import requests
import urllib3.exceptions

from . import config
from .gmail_client import get_credentials

_gc = None

# 一時的なネットワークエラーとみなし、自動リトライの対象にする例外。
# ・requests.exceptions.SSLError / ConnectionError / Timeout: Wi-Fi瞬断やVPNなどによる通信断
# ・urllib3.exceptions.MaxRetryError: urllib3自体のリトライも尽きた場合
# ・gspread.exceptions.APIError: Google側の一時的なエラー応答（5xx等）
_RETRYABLE_EXCEPTIONS = (
    requests.exceptions.SSLError,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    urllib3.exceptions.MaxRetryError,
    gspread.exceptions.APIError,
)


def _call_with_retry(func, *args, max_attempts: int = 4, **kwargs):
    """
    Google Sheets APIへの呼び出しを、一時的な通信エラーに対する自動リトライ付きで実行する。
    2, 4, 8秒...と間隔を空けながら最大max_attempts回まで試し、それでも失敗すれば
    最後の例外をそのまま呼び出し元に投げる（無限リトライにはしない）。
    """
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            return func(*args, **kwargs)
        except _RETRYABLE_EXCEPTIONS as e:
            last_error = e
            if attempt == max_attempts:
                break
            wait_seconds = 2 ** attempt  # 2, 4, 8, 16秒...
            print(
                f"⚠️ Google Sheets APIへの通信に失敗しました（{attempt}/{max_attempts}回目）。"
                f"{wait_seconds}秒待ってリトライします: {e}"
            )
            time.sleep(wait_seconds)
    raise last_error


def _client():
    global _gc
    if _gc is None:
        _gc = _call_with_retry(gspread.authorize, get_credentials())
    return _gc


def _spreadsheet():
    return _call_with_retry(_client().open_by_key, config.GOOGLE_SHEET_ID)


def _parse_yen(raw) -> int:
    """「1,234」「-1,234」「(1,234)」のいずれの表記でも整数に変換する。"""
    s = str(raw).replace(",", "").strip()
    if not s:
        return 0
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    return int(s or 0)


def _parse_detail_date(raw: str):
    """「取引明細」B列の日時文字列をdatetimeに変換する。読めなければNone。"""
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, fmt)
        except (ValueError, TypeError):
            continue
    return None


# ============================================================
# 取引明細・ログ
# ============================================================

def append_transaction(effective_date: datetime, merchant: str, amount: int, category: str) -> None:
    """「取引明細」シートに1行追加する（GASのdetailSheet.appendRowに相当）。
    A列を空けたまま使う設計のため、append_row(table_range="A1")のような
    「テーブル自動検出」に頼る書き方はしない（A列が常に空のため、Google側が
    テーブルを誤検出し、ヘッダー行を無視して先頭行に上書きしてしまうことがあった）。
    現在の行数を明示的に数え、必要なら行を追加してから、その行にだけ直接書き込む。"""
    sheet = _spreadsheet().worksheet(config.SHEET_DETAIL)
    row_number = len(_call_with_retry(sheet.get_all_values)) + 1
    if sheet.row_count < row_number:
        _call_with_retry(sheet.add_rows, row_number - sheet.row_count)
    _call_with_retry(
        sheet.update,
        f"A{row_number}",
        [["", effective_date.strftime("%Y/%m/%d %H:%M:%S"), merchant, amount, category]],
        value_input_option="USER_ENTERED",
    )


def add_to_log(date_key: str, amount_to_add: int) -> None:
    """「ログ」シートの該当日の行に金額を加算する（無ければ新規作成する）。GASのaddToLogを移植。
    新規作成時も、append_transactionと同じ理由でappend_rowは使わず、行数を明示的に
    数えてから直接該当行に書き込む。"""
    sheet = _spreadsheet().worksheet(config.SHEET_LOG)
    records = _call_with_retry(sheet.get_all_values)

    target_row = None
    for i, row in enumerate(records):
        if len(row) > 1 and row[1] == date_key:
            target_row = i + 1  # gspreadは1始まり
            break

    if target_row:
        current = _call_with_retry(sheet.cell, target_row, 3).value or "0"
        current_value = _parse_yen(current)
        _call_with_retry(sheet.update_cell, target_row, 3, current_value + amount_to_add)
    else:
        row_number = len(records) + 1
        if sheet.row_count < row_number:
            _call_with_retry(sheet.add_rows, row_number - sheet.row_count)
        _call_with_retry(
            sheet.update,
            f"A{row_number}",
            [["", date_key, amount_to_add]],
            value_input_option="USER_ENTERED",
        )


def get_log_records() -> list:
    """「ログ」シートの全行を返す。"""
    sheet = _spreadsheet().worksheet(config.SHEET_LOG)
    return _call_with_retry(sheet.get_all_values)


def get_detail_records() -> list:
    """「取引明細」シートの全行を返す。各行は [A, 日時, 店舗名, 金額, カテゴリ] の並び。"""
    sheet = _spreadsheet().worksheet(config.SHEET_DETAIL)
    return _call_with_retry(sheet.get_all_values)


def get_parsed_detail_rows() -> list[dict]:
    """「取引明細」を、日付をdatetimeにパース済みの辞書リストに変換して返す。
    日付が読めない行（ヘッダー・空行）は自動的に除外される。"""
    rows = []
    for row in get_detail_records():
        if len(row) < 4:
            continue
        row_date = _parse_detail_date(row[1])
        if not row_date:
            continue
        amount_raw = str(row[3]).strip()
        if not amount_raw:
            continue
        rows.append({
            "date": row_date,
            "merchant": row[2] if len(row) > 2 else "",
            "amount": _parse_yen(amount_raw),
            "category": row[4] if len(row) > 4 and row[4] else config.DEFAULT_CATEGORY,
        })
    return rows


# ============================================================
# 予算計画
# ============================================================

def get_monthly_budget(target_date: datetime) -> dict | None:
    """「予算計画」シートから、指定した年月（B列「yyyy年M月」）の行を探す。
    見つかれば {row, budget}（C列＝計画）、無ければNone。"""
    sheet = _spreadsheet().worksheet(config.SHEET_BUDGET)
    records = _call_with_retry(sheet.get_all_values)
    for i, row in enumerate(records):
        if len(row) < 2:
            continue
        match = re.match(r"(\d{4})年(\d{1,2})月", row[1])  # B列
        if not match:
            continue
        y, m = int(match.group(1)), int(match.group(2))
        if y == target_date.year and m == target_date.month:
            budget = _parse_yen(row[2]) if len(row) > 2 else 0  # C列
            return {"row": i + 1, "budget": budget}
    return None


def _latest_budget_before(target_date: datetime) -> dict | None:
    """「予算計画」シートの中から、target_dateより前で一番新しい年月の行を探す。"""
    sheet = _spreadsheet().worksheet(config.SHEET_BUDGET)
    records = _call_with_retry(sheet.get_all_values)
    target_key = (target_date.year, target_date.month)
    best = None
    for row in records:
        if len(row) < 2:
            continue
        match = re.match(r"(\d{4})年(\d{1,2})月", row[1])
        if not match:
            continue
        key = (int(match.group(1)), int(match.group(2)))
        if key < target_key and (best is None or key > best[0]):
            best = (key, _parse_yen(row[2]) if len(row) > 2 else 0)
    return {"budget": best[1]} if best else None


def ensure_monthly_budget_row(target_date: datetime) -> dict | None:
    """該当年月の行が無ければ、それより前で一番新しい月の「計画」額をコピーして自動で行を追加する。
    それより前の行が1つも無ければNoneを返す（何もしない）。

    ■ 雛形化での変更点
    旧版は「すぐ前の月」の行だけを見ていたため、間の月が抜けていると（例：初期設定で
    9月の行だけがあり、必要なのは引落し月の11月）、いつまでも自動追加されなかった。"""
    prev = _latest_budget_before(target_date)
    if not prev:
        return None

    sheet = _spreadsheet().worksheet(config.SHEET_BUDGET)
    month_label = f"{target_date.year}年{target_date.month}月"
    new_row = len(_call_with_retry(sheet.get_all_values)) + 1
    _call_with_retry(sheet.update_cell, new_row, 2, month_label)   # B列
    _call_with_retry(sheet.update_cell, new_row, 3, prev["budget"])  # C列
    apply_budget_sheet_borders()
    return {"row": new_row, "budget": prev["budget"]}


def update_budget_actuals(row: int, actual: int, diff: int) -> None:
    """「予算計画」シートのE列(実績・月)・G列(差額・月)を更新し、F・H列(累計)を再計算する。"""
    sheet = _spreadsheet().worksheet(config.SHEET_BUDGET)
    _call_with_retry(sheet.update_cell, row, 5, actual)  # E列
    _call_with_retry(sheet.update_cell, row, 7, diff)    # G列
    _recalculate_budget_cumulative(sheet)
    apply_budget_sheet_borders()


def set_closing_actual(row: int, closing_total: int) -> None:
    """「予算計画」シートのI列(確定実績・締め)を更新する。"""
    sheet = _spreadsheet().worksheet(config.SHEET_BUDGET)
    _call_with_retry(sheet.update_cell, row, 9, closing_total)  # I列


def apply_budget_sheet_borders() -> None:
    """「予算計画」のB2:I(最終行)に全罫線を引く（GASのapplyBudgetSheetBordersに相当。
    Python移植時にこの呼び出しが抜けており、行を追加・更新しても枠線が伸びない
    不具合があったため追加した）。"""
    sheet = _spreadsheet().worksheet(config.SHEET_BUDGET)
    last_row = len(_call_with_retry(sheet.get_all_values))
    if last_row < 2:
        return
    _apply_borders(sheet, start_row=2, end_row=last_row, start_col=2, end_col=9)  # B〜I列


def _recalculate_budget_cumulative(sheet) -> None:
    """F列(実績累計)・H列(差額累計)を、データ開始行から最終行まで積み上げ直す。

    ■ 雛形化での変更点
    旧版はデータ開始行を「4行目」に固定していたが、initial_setup.py で作ったシートは
    3行目からデータが始まるため、最初の月が累計から漏れていた。B列が「yyyy年M月」に
    なっている最初の行を、データ開始行として自動で探すようにした。"""
    values = _call_with_retry(sheet.get_all_values)
    last_row = len(values)
    first_index = next(
        (i for i, row in enumerate(values) if len(row) > 1 and re.match(r"\d{4}年\d{1,2}月", row[1])),
        None,
    )
    if first_index is None:
        return
    first_row = first_index + 1

    e_cum = 0
    h_cum = 0
    f_updates = []
    h_updates = []
    for i in range(first_index, last_row):
        row = values[i]
        e_raw = row[4] if len(row) > 4 else ""   # E列
        g_raw = row[6] if len(row) > 6 else ""   # G列
        if e_raw not in ("", None):
            e_cum += _parse_yen(e_raw)
            h_cum += _parse_yen(g_raw)
            f_updates.append(e_cum)
            h_updates.append(h_cum)
        else:
            f_updates.append("")
            h_updates.append("")

    # 1セルずつ書くと月数×2回のAPI呼び出しになり、書き込み回数制限（1分あたり60回）に
    # 引っかかりやすいため、F列・H列それぞれ1回の呼び出しでまとめて書き込む
    if f_updates:
        end_row = first_row + len(f_updates) - 1
        _call_with_retry(sheet.update, f"F{first_row}:F{end_row}", [[v] for v in f_updates])  # F列
        _call_with_retry(sheet.update, f"H{first_row}:H{end_row}", [[v] for v in h_updates])  # H列


# ============================================================
# カテゴリルール（店舗名→カテゴリの判定キーワード）
# ============================================================

SHEET_CATEGORY_RULES = getattr(config, "SHEET_CATEGORY_RULES", "カテゴリルール")
_KEYWORD_SPLIT_PATTERN = re.compile(r"[,，、]")
_category_rules_cache: list[tuple[str, list[str]]] | None = None


def ensure_category_rules_sheet():
    """「カテゴリルール」シートが無ければ、config.CATEGORY_RULES（初期値）の内容で作成する。
    シート構成：1行目は空欄、2行目がヘッダー、3行目以降が
    B列=カテゴリ名、C列=キーワード（カンマ区切り）。上の行ほど優先して判定される。"""
    ss = _spreadsheet()
    try:
        return ss.worksheet(SHEET_CATEGORY_RULES)
    except gspread.exceptions.WorksheetNotFound:
        pass

    rows = [[name, ", ".join(keywords)] for name, keywords in config.CATEGORY_RULES]
    sheet = _call_with_retry(
        ss.add_worksheet, title=SHEET_CATEGORY_RULES, rows=max(len(rows) + 20, 30), cols=5
    )
    _call_with_retry(
        sheet.update, "B2:D2",
        [["カテゴリ", "キーワード（カンマ区切り）", "← 店舗名にキーワードが含まれていれば、そのカテゴリに分類されます。上の行ほど優先。"]],
    )
    if rows:
        _call_with_retry(sheet.update, f"B3:C{2 + len(rows)}", rows, value_input_option="RAW")
    _call_with_retry(sheet.format, "B2:C2", {"textFormat": {"bold": True}})
    _apply_borders(sheet, start_row=2, end_row=2 + len(rows), start_col=2, end_col=3)
    print(f"「{SHEET_CATEGORY_RULES}」シートを作成しました。")
    return sheet


def get_category_rules() -> list[tuple[str, list[str]]]:
    """
    「カテゴリルール」シートから [(カテゴリ名, [キーワード, ...]), ...] を読み込む
    （1回の実行内ではキャッシュする）。シートが空・読み込みに失敗した場合は
    config.CATEGORY_RULES（初期値）を使う（カテゴリ判定が止まるとメール処理全体が
    止まってしまうため、ここでは例外を外に出さない）。
    """
    global _category_rules_cache
    if _category_rules_cache is not None:
        return _category_rules_cache

    rules: list[tuple[str, list[str]]] = []
    try:
        sheet = ensure_category_rules_sheet()
        for row in _call_with_retry(sheet.get_all_values)[2:]:  # 3行目以降がデータ
            name = str(row[1]).strip() if len(row) > 1 else ""
            raw_keywords = str(row[2]) if len(row) > 2 else ""
            keywords = [k.strip() for k in _KEYWORD_SPLIT_PATTERN.split(raw_keywords) if k.strip()]
            if name and keywords:
                rules.append((name, keywords))
    except Exception as e:
        print(f"「{SHEET_CATEGORY_RULES}」シートを読み込めなかったため、初期設定のルールを使います: {e}")

    _category_rules_cache = rules or list(config.CATEGORY_RULES)
    return _category_rules_cache


# ============================================================
# カテゴリ予算
# ============================================================

def ensure_category_budget_sheet():
    """「カテゴリ予算」シートが無ければ、CATEGORY_RULESの各カテゴリ名で作成する。
    既にあれば、除外カテゴリ（通信費・保険）の行が残っていれば削除し、枠線を更新する
    （GASのensureCategoryBudgetSheetに相当。既存シートに対しても毎回このクリーンアップを
    行う点がPython移植時に抜けていたため追加した）。"""
    ss = _spreadsheet()
    try:
        sheet = ss.worksheet(config.SHEET_CATEGORY_BUDGET)
        _remove_excluded_category_budget_rows(sheet)
        apply_category_budget_sheet_borders()
        return sheet
    except gspread.exceptions.WorksheetNotFound:
        sheet = _call_with_retry(ss.add_worksheet, title=config.SHEET_CATEGORY_BUDGET, rows=20, cols=5)
        _call_with_retry(sheet.update, "B2:C2", [["カテゴリ", "月間予算(円)"]])
        rows = [
            [name, 0]
            for name, _ in get_category_rules()
            if name not in config.CATEGORY_BUDGET_EXCLUDED
        ]
        if rows:
            _call_with_retry(sheet.update, f"B3:C{2 + len(rows)}", rows)
        apply_category_budget_sheet_borders()
        return sheet


def _remove_excluded_category_budget_rows(sheet) -> None:
    """「カテゴリ予算」シートの中から、除外対象カテゴリ（通信費・保険など）の
    行が残っていれば削除する（GASのremoveExcludedCategoryBudgetRowsに相当）。"""
    records = _call_with_retry(sheet.get_all_values)
    rows_to_delete = []
    for i, row in enumerate(records):
        if i < 2:  # 1〜2行目はヘッダー
            continue
        name = str(row[1]).strip() if len(row) > 1 else ""
        if name in config.CATEGORY_BUDGET_EXCLUDED:
            rows_to_delete.append(i + 1)
    for row_number in sorted(rows_to_delete, reverse=True):
        _call_with_retry(sheet.delete_rows, row_number)


def apply_category_budget_sheet_borders() -> None:
    """「カテゴリ予算」のB2:C(最終行)に全罫線を引く（GASのapplyCategoryBudgetSheetBordersに相当）。"""
    sheet = _spreadsheet().worksheet(config.SHEET_CATEGORY_BUDGET)
    last_row = len(_call_with_retry(sheet.get_all_values))
    if last_row < 2:
        return
    _apply_borders(sheet, start_row=2, end_row=last_row, start_col=2, end_col=3)  # B〜C列


def get_category_budgets() -> dict:
    """「カテゴリ予算」シートから、予算が0より大きいカテゴリだけを {カテゴリ名: 予算} で返す。"""
    sheet = ensure_category_budget_sheet()
    records = _call_with_retry(sheet.get_all_values)
    budgets = {}
    for row in records[2:]:  # 3行目以降がデータ
        if len(row) < 3:
            continue
        name = str(row[1]).strip()
        budget = _parse_yen(row[2])
        if name and budget > 0:
            budgets[name] = budget
    return budgets


# ============================================================
# 日次利用額（見やすい形に再構築する表示用シート）
# ============================================================

def rebuild_daily_usage_sheet() -> None:
    """「日次利用額」シートを、「取引明細」の内容から丸ごと作り直す（GASのrebuildDailyUsageSheetを移植）。
    日付ごとに、まとめ行（合計・件数）とその内訳（店舗名・金額・カテゴリ）を縦に積んで表示する。

    ■ 2026/09/16 修正
    sheet.clear()はセルの「中身」だけを消し、太字・罫線などの「書式」は消さない
    ため、過去に太字だったセル位置に新しいデータを書き込むと、本来まとめ行だけに
    付けたい太字が無関係な内訳行に残ってしまう不具合があった（GAS版にあった
    「まとめ行だけを太字にする」処理自体もPython移植時に抜けていた）。
    書き込み後に全体の太字を一度リセットし、まとめ行（日付ごとの合計行）だけに
    改めて太字を設定し、枠線も引き直すようにした。"""
    rows = get_parsed_detail_rows()
    tz_label = os.environ.get("TZ", "UTC")

    by_date: dict[str, list[dict]] = {}
    for r in rows:
        date_key = r["date"].strftime("%Y/%m/%d")
        by_date.setdefault(date_key, []).append(r)

    # 同じ日の中では時刻が新しい順、日付自体も新しい順に並べる
    for entries in by_date.values():
        entries.sort(key=lambda e: e["date"], reverse=True)
    date_keys_desc = sorted(by_date.keys(), reverse=True)

    new_rows = [["日付（現地）", "合計金額(円)", "件数", "店舗名", "金額", "カテゴリ", "タイムゾーン"]]
    summary_row_numbers = [1]  # ヘッダー行（1行目）は常に太字
    for date_key in date_keys_desc:
        entries = by_date[date_key]
        total = sum(e["amount"] for e in entries)
        new_rows.append([date_key, total, len(entries), "", "", "", tz_label])
        summary_row_numbers.append(len(new_rows))  # 今追加したまとめ行の行番号
        for e in entries:
            new_rows.append(["", "", "", e["merchant"], e["amount"], e["category"], ""])

    ss = _spreadsheet()
    try:
        sheet = ss.worksheet(config.SHEET_DAILY_USAGE)
    except Exception:
        sheet = _call_with_retry(
            ss.add_worksheet, title=config.SHEET_DAILY_USAGE, rows=max(len(new_rows) + 10, 100), cols=7
        )

    _call_with_retry(sheet.clear)
    _call_with_retry(sheet.update, "A1", new_rows, value_input_option="USER_ENTERED")

    last_row = len(new_rows)

    # まず全体の太字をいったんリセットし、その後まとめ行（ヘッダー＋日付ごとの合計行）だけ
    # 太字にする（過去の書式が無関係な行に残るのを防ぐ）
    _call_with_retry(
        sheet.spreadsheet.batch_update,
        {
            "requests": [{
                "repeatCell": {
                    "range": {"sheetId": sheet.id, "startRowIndex": 0, "endRowIndex": last_row,
                               "startColumnIndex": 0, "endColumnIndex": 7},
                    "cell": {"userEnteredFormat": {"textFormat": {"bold": False}}},
                    "fields": "userEnteredFormat.textFormat.bold",
                }
            }] + [
                {
                    "repeatCell": {
                        "range": {"sheetId": sheet.id, "startRowIndex": row_number - 1, "endRowIndex": row_number,
                                   "startColumnIndex": 0, "endColumnIndex": 7},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat.bold",
                    }
                }
                for row_number in summary_row_numbers
            ]
        },
    )

    _apply_borders(sheet, start_row=1, end_row=last_row, start_col=1, end_col=7)


# ============================================================
# 重複チェック（近い重複の警告。完全一致は自動整理する）
# ============================================================

def cleanup_duplicate_transactions() -> tuple[int, list[str]]:
    """
    「取引明細」の中から、日時・店舗名・金額が完全に一致する行（＝同じメールが
    誤って何度も処理されたもの）を見つけ、1件だけ残して残りを削除する。
    削除後、影響を受けた日付の「ログ」シートの金額を、取引明細から作り直す。
    似ているだけで完全一致ではない行（金額が近い・時間が近いだけ）は対象外
    （誤って正しい取引を消さないための安全策。そちらは check_near_duplicates で警告のみ行う）。
    @return (削除した件数, 影響を受けた日付のリスト)
    """
    sheet = _spreadsheet().worksheet(config.SHEET_DETAIL)
    records = _call_with_retry(sheet.get_all_values)

    seen: dict[str, int] = {}
    rows_to_delete: list[int] = []
    affected_dates: set[str] = set()

    for i, row in enumerate(records):
        if len(row) < 4:
            continue
        raw_date, merchant, amount_raw = row[1], row[2], row[3]
        if not raw_date or not amount_raw:
            continue
        key = f"{raw_date}__{merchant}__{amount_raw}"
        row_number = i + 1
        if key in seen:
            rows_to_delete.append(row_number)
            affected_dates.add(raw_date.split(" ")[0])
        else:
            seen[key] = row_number

    if not rows_to_delete:
        return 0, []

    for row_number in sorted(rows_to_delete, reverse=True):
        _call_with_retry(sheet.delete_rows, row_number)

    _recalculate_log_totals_for_dates(sorted(affected_dates))

    return len(rows_to_delete), sorted(affected_dates)


def _recalculate_log_totals_for_dates(date_keys: list[str]) -> None:
    """指定した日付（yyyy/MM/dd の配列）について、「ログ」シートのその日の
    合計を、「取引明細」の現在の内容から作り直す（重複削除の後始末用）。"""
    if not date_keys:
        return

    totals: dict[str, int] = {k: 0 for k in date_keys}
    for row in get_detail_records():
        if len(row) < 4:
            continue
        raw_date, amount_raw = row[1], row[3]
        if not raw_date or not amount_raw:
            continue
        date_part = raw_date.split(" ")[0]
        if date_part in totals:
            totals[date_part] += _parse_yen(amount_raw)

    log_sheet = _spreadsheet().worksheet(config.SHEET_LOG)
    log_records = _call_with_retry(log_sheet.get_all_values)

    for date_key, total in totals.items():
        target_row = None
        for i, row in enumerate(log_records):
            if len(row) > 1 and row[1] == date_key:
                target_row = i + 1
                break
        if target_row:
            _call_with_retry(log_sheet.update_cell, target_row, 3, total)


_TRAILING_SYMBOLS_PATTERN = re.compile(r"[\s\*＊]+$")
# 末尾の（...）を取り除く。為替換算メモ「（12.34USD（1USD=150円換算））」のように
# 括弧が1段ネストすることがあるため、内側に1段だけ別の（...）を許容する。
_TRAILING_PAREN_PATTERN = re.compile(r"^(.*?\S)\s*（(?:[^（）]|（[^（）]*）)*）$")


def _merchant_core_name(merchant: str) -> str:
    """
    店舗名から、末尾の為替換算メモ「（12.34USD（1USD=150円換算））」や
    返金タグ「（返金）」、末尾の記号（*、＊、空白）を取り除いた「核」となる名前を返す。
    Vpassの即時通知（外貨建て）と確定通知（円建て）が同じ買い物を指している場合に、
    突き合わせるための比較キーとして使う。

    ■ 2026/09/15 修正
    確定通知メールは店舗名の英字が「ＴＡＲＧＥＴ．ＣＯＭ」のように全角で
    書かれてくることがあり、即時通知（半角）とは文字コード上一致しないため、
    同じ店舗として突き合わせられなかった。括弧・記号を取り除いた後、
    unicodedata.normalize("NFKC", ...)で全角英数字を半角に正規化してから比較する。
    """
    name = merchant
    while True:
        m = _TRAILING_PAREN_PATTERN.match(name)
        if not m:
            break
        name = m.group(1)
    name = _TRAILING_SYMBOLS_PATTERN.sub("", name).strip()
    name = unicodedata.normalize("NFKC", name)
    return name.upper()


def reconcile_quick_and_confirmed_transactions() -> tuple[int, list[str]]:
    """
    Vpassの「即時通知」（外貨建て、利用直後に届く。金額は市場レートでの推定換算）と
    「確定通知」（円建て、数日後に届く。金額はカード会社が実際に決済した確定額）が、
    同じ実際の買い物について二重に記録されてしまっている場合に、1件にまとめる。

    同じ日付・同じ店舗名（為替メモや末尾の記号の違いは無視）で、即時通知1件・
    確定通知1件がちょうどペアで見つかった場合だけを対象にする（同じ店舗に同じ日に
    複数回行った場合など、1対1にならないケースは誤って削除しないよう対象外にする）。
    確定通知の金額の方が実際の決済レートに基づく正しい金額なので、そちらを残し、
    即時通知（推定レート換算）の行を削除する。
    @return (削除した件数, 影響を受けた日付のリスト)
    """
    sheet = _spreadsheet().worksheet(config.SHEET_DETAIL)
    records = _call_with_retry(sheet.get_all_values)

    entries = []
    for i, row in enumerate(records):
        if len(row) < 4:
            continue
        raw_date, merchant, amount_raw = row[1], row[2], row[3]
        if not raw_date or not amount_raw:
            continue
        entries.append({
            "row": i + 1,
            "date": raw_date.split(" ")[0],
            "core": _merchant_core_name(merchant),
            "is_quick": "円換算）" in merchant,
        })

    groups: dict[tuple, list[dict]] = {}
    for e in entries:
        groups.setdefault((e["date"], e["core"]), []).append(e)

    rows_to_delete: list[int] = []
    affected_dates: set[str] = set()

    for (date_key, _core), group in groups.items():
        quick_ones = [e for e in group if e["is_quick"]]
        confirmed_ones = [e for e in group if not e["is_quick"]]
        if len(quick_ones) == 1 and len(confirmed_ones) == 1:
            rows_to_delete.append(quick_ones[0]["row"])
            affected_dates.add(date_key)

    if not rows_to_delete:
        return 0, []

    for row_number in sorted(rows_to_delete, reverse=True):
        _call_with_retry(sheet.delete_rows, row_number)

    _recalculate_log_totals_for_dates(sorted(affected_dates))

    return len(rows_to_delete), sorted(affected_dates)


def cleanup_same_moment_duplicates() -> tuple[int, list[str]]:
    """
    「取引明細」の中から、日時（分・秒まで完全一致）・店舗の核名（為替メモや記号を
    除いた部分）が同じ行を探す。完全一致の重複整理（cleanup_duplicate_transactions）は
    金額も一致していることを条件にしているが、全期間の再構築を日をまたいで複数回
    行うと、同じ買い物がその都度の為替レートで微妙に異なる金額として記録され、
    金額不一致のせいで重複と判定されずに残ってしまうことがあった（例：同じ時刻の
    同じ店舗が「1USD=154円換算」と「1USD=155円換算」で2行に分かれる）。
    金額は問わず、日時・店舗の核名だけで同一とみなし、最後の1件だけ残して整理する。
    @return (削除した件数, 影響を受けた日付のリスト)
    """
    sheet = _spreadsheet().worksheet(config.SHEET_DETAIL)
    records = _call_with_retry(sheet.get_all_values)

    groups: dict[tuple, list[int]] = {}
    for i, row in enumerate(records):
        if len(row) < 4:
            continue
        raw_date, merchant = row[1], row[2]
        if not raw_date or not merchant:
            continue
        key = (raw_date, _merchant_core_name(merchant))
        groups.setdefault(key, []).append(i + 1)

    rows_to_delete: list[int] = []
    affected_dates: set[str] = set()
    for (raw_date, _core), row_numbers in groups.items():
        if len(row_numbers) < 2:
            continue
        for row_number in row_numbers[:-1]:  # 最後（最大の行番号）だけ残す
            rows_to_delete.append(row_number)
        affected_dates.add(raw_date.split(" ")[0])

    if not rows_to_delete:
        return 0, []

    for row_number in sorted(rows_to_delete, reverse=True):
        _call_with_retry(sheet.delete_rows, row_number)

    _recalculate_log_totals_for_dates(sorted(affected_dates))
    return len(rows_to_delete), sorted(affected_dates)


def _near_duplicate_pair_key(row_a: dict, row_b: dict) -> str:
    """近い重複のペアを一意に識別するキーを作る。行番号は並べ替えのたびに変わって
    しまうため使わず、両者の日時（分単位まで一致していれば同一取引とみなせる粒度）と
    店舗の素の名前を組み合わせる。"""
    a_key = row_a["date"].isoformat()
    b_key = row_b["date"].isoformat()
    first, second = sorted([a_key, b_key])
    return f"{first}|{second}|{row_a['base_name']}"


def _get_warned_duplicate_keys() -> set[str]:
    raw = get_state("near_duplicate_warned")
    if not raw:
        return set()
    try:
        return set(json.loads(raw))
    except (ValueError, TypeError):
        return set()


def _save_warned_duplicate_keys(keys: set[str]) -> None:
    set_state("near_duplicate_warned", json.dumps(sorted(keys)))


def find_near_duplicate_warnings() -> list[str]:
    """
    「取引明細」の中から、金額の差3%以内・時刻の差90分以内・店舗名（注記を除いた素の名前）が
    一致する「近い重複」を探し、警告文のリストを返す（GASのcheckForDuplicatesAndWarnを移植）。
    自動削除はしない。判断は人が行う。

    ■ 2026/09/18 修正
    このチェックは実行のたびに「取引明細」全体を毎回スキャンする仕様のため、
    15分おきの自動実行では、何年も前からある未解決の「近い重複」が、人が対応する
    まで解決されず、毎回・永遠に警告され続けてしまっていた（1日に何十回も同じ
    警告が届き、実質的に見なくなってしまう状態になっていた）。
    一度警告した組み合わせ（日時のペア）を「システム状態」シートに記録しておき、
    次回以降は既に警告済みの組み合わせを除外して、新しく見つかったものだけを返す
    ようにした（対応済みかどうかに関わらず、同じ組み合わせを再度警告することはない）。
    """
    rows = get_parsed_detail_rows()

    def base_name(merchant: str) -> str:
        idx = merchant.find("（")
        return (merchant[:idx] if idx != -1 else merchant).strip()

    by_day: dict[str, list[dict]] = {}
    for r in rows:
        day_key = r["date"].strftime("%Y-%m-%d")
        by_day.setdefault(day_key, []).append({**r, "base_name": base_name(r["merchant"])})

    already_warned = _get_warned_duplicate_keys()
    current_keys: set[str] = set()
    warnings = []
    for day_key, day_rows in by_day.items():
        for a in range(len(day_rows)):
            for b in range(a + 1, len(day_rows)):
                row_a, row_b = day_rows[a], day_rows[b]
                if row_a["amount"] == 0 or row_b["amount"] == 0:
                    continue
                amount_diff_ratio = abs(row_a["amount"] - row_b["amount"]) / max(abs(row_a["amount"]), abs(row_b["amount"]))
                minutes_diff = abs((row_a["date"] - row_b["date"]).total_seconds()) / 60
                is_near_duplicate = (
                    amount_diff_ratio <= 0.03
                    and minutes_diff <= 90
                    and row_a["base_name"] == row_b["base_name"]
                    and row_a["base_name"]
                )
                if not is_near_duplicate:
                    continue

                pair_key = _near_duplicate_pair_key(row_a, row_b)
                current_keys.add(pair_key)
                if pair_key in already_warned:
                    continue  # 既に一度警告済みなので、今回は知らせない
                warnings.append(
                    f"{day_key}　「{row_a['base_name']}」¥{row_a['amount']:,} ⇔ ¥{row_b['amount']:,}"
                )

    _save_warned_duplicate_keys(current_keys)
    return warnings


# ============================================================
# 未分類店舗（カテゴリ「その他」）の集計
# ============================================================

def get_unclassified_merchants_summary() -> list[tuple[str, dict]]:
    """「その他」に分類されている店舗名（注記を除いた素の店名）を、件数・合計金額とともに
    集計する。合計金額が多い順のリストを返す。"""

    def base_name(merchant: str) -> str:
        idx = merchant.find("（")
        return (merchant[:idx] if idx != -1 else merchant).strip()

    unclassified: dict[str, dict] = {}
    for r in get_parsed_detail_rows():
        if r["category"] != config.DEFAULT_CATEGORY:
            continue
        name = base_name(r["merchant"])
        if not name:
            continue
        entry = unclassified.setdefault(name, {"count": 0, "total": 0})
        entry["count"] += 1
        entry["total"] += r["amount"]

    return sorted(unclassified.items(), key=lambda kv: kv[1]["total"], reverse=True)


# ============================================================
# 年間サマリー
# ============================================================

def build_annual_summary_sheet() -> dict:
    """「年間サマリー」シートに、月×カテゴリのクロス集計表を作る（GASのbuildAnnualSummarySheetを移植）。
    @return {"months": 対象月数, "categories": カテゴリ数}"""
    rows = get_parsed_detail_rows()

    totals_by_month: dict[str, dict[str, int]] = {}
    all_categories: set[str] = set()
    all_months: set[str] = set()

    for r in rows:
        month_key = r["date"].strftime("%Y/%m")
        totals_by_month.setdefault(month_key, {})
        totals_by_month[month_key][r["category"]] = totals_by_month[month_key].get(r["category"], 0) + r["amount"]
        all_categories.add(r["category"])
        all_months.add(month_key)

    sorted_months = sorted(all_months)
    # 「カテゴリルール」シートに書かれている順に並べ、そこに無いカテゴリは後ろに付け足す
    display_order = [
        name for name, _ in get_category_rules()
        if name in all_categories and name != config.DEFAULT_CATEGORY
    ]
    sorted_categories = display_order.copy()
    for cat in sorted(all_categories):
        if cat not in display_order and cat != config.DEFAULT_CATEGORY:
            sorted_categories.append(cat)
    sorted_categories.append(config.DEFAULT_CATEGORY)

    ss = _spreadsheet()
    try:
        sheet = ss.worksheet(config.SHEET_ANNUAL_SUMMARY)
        _call_with_retry(sheet.clear)
    except Exception:
        sheet = _call_with_retry(ss.add_worksheet, title=config.SHEET_ANNUAL_SUMMARY, rows=100, cols=20)

    if not sorted_months:
        _call_with_retry(sheet.update, "A1", [["まだ集計できるデータがありません。"]])
        return {"months": 0, "categories": 0}

    header = ["年月"] + sorted_categories + ["合計"]
    table = [header]
    grand_totals = [0] * len(sorted_categories)
    grand_total = 0

    for month_key in sorted_months:
        month_totals = totals_by_month.get(month_key, {})
        row_values = []
        row_total = 0
        for idx, cat in enumerate(sorted_categories):
            v = month_totals.get(cat, 0)
            row_values.append(v)
            row_total += v
            grand_totals[idx] += v
        grand_total += row_total
        table.append([month_key] + row_values + [row_total])

    table.append(["年間合計"] + grand_totals + [grand_total])

    _call_with_retry(sheet.update, "A1", table, value_input_option="USER_ENTERED")

    _move_sheet_after(sheet, config.SHEET_LOG)

    return {"months": len(sorted_months), "categories": len(sorted_categories)}


def _move_sheet_after(sheet, after_sheet_name: str) -> None:
    """指定したシートのタブを、after_sheet_nameのシートのすぐ右へ移動する
    （GASのbuildAnnualSummarySheet末尾にあるタブ位置調整に相当。年間サマリーが
    「ログ」の右隣に来るよう、実行のたびに揃える）。失敗しても致命的ではないため、
    エラーは無視する。"""
    try:
        ss = _spreadsheet()
        after_sheet = ss.worksheet(after_sheet_name)
        _call_with_retry(
            ss.batch_update,
            {
                "requests": [{
                    "updateSheetProperties": {
                        "properties": {"sheetId": sheet.id, "index": after_sheet.index + 1},
                        "fields": "index",
                    }
                }]
            },
        )
    except Exception as e:
        print(f"年間サマリーのタブ位置調整に失敗しました（実害はありません）: {e}")


def backup_sheet(sheet_name: str) -> str:
    """指定したシートの現在の内容を丸ごと複製し、別名で保存する（安全のためのバックアップ）。
    複製後のシート名を返す。全期間再構築（rebuild_all_transactions.py）の前処理として使う。"""
    ss = _spreadsheet()
    source = ss.worksheet(sheet_name)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"{sheet_name}_backup_{timestamp}"
    duplicated = _call_with_retry(source.duplicate, new_sheet_name=backup_name)
    return duplicated.title


def bulk_append_transactions(rows: list[tuple]) -> None:
    """[(effective_date, merchant, amount, category), ...] を「取引明細」に
    まとめて書き込む。

    ■ 2026/09/15 修正
    以前はappend_rows(table_range="A1")を使っていたが、このシートはA列を
    意図的に空けたまま使う設計のため、Googleの「テーブル自動検出」がA列を見て
    「テーブルは空」と誤判定し、2行目のヘッダーを無視して1行目からデータを
    上書きしてしまう不具合があった。現在の行数を明示的に数えてから必要な分だけ
    シートを拡張し（add_rows）、その直後の行範囲に直接updateで書き込むことで、
    ヘッダーを壊さず、かつ行数不足によるエラー（"exceeds grid limits"）も
    避けるようにした。"""
    if not rows:
        return
    sheet = _spreadsheet().worksheet(config.SHEET_DETAIL)
    values = [
        ["", d.strftime("%Y/%m/%d %H:%M:%S"), merchant, amount, category]
        for d, merchant, amount, category in rows
    ]
    start_row = len(_call_with_retry(sheet.get_all_values)) + 1
    end_row = start_row + len(values) - 1
    if sheet.row_count < end_row:
        _call_with_retry(sheet.add_rows, end_row - sheet.row_count)
    _call_with_retry(sheet.update, f"A{start_row}", values, value_input_option="USER_ENTERED")


def bulk_write_log(date_totals: dict) -> None:
    """{date_key(yyyy/MM/dd): 合計金額} を「ログ」にまとめて書き込む。
    bulk_append_transactionsと同じ理由で、append_rowsではなく明示的な
    行拡張＋updateを使う。clear_detail_and_logで空にした直後に使う前提
    （既存行との突き合わせはしない）。"""
    if not date_totals:
        return
    sheet = _spreadsheet().worksheet(config.SHEET_LOG)
    sorted_dates = sorted(date_totals.keys())
    values = [["", date_key, date_totals[date_key]] for date_key in sorted_dates]
    start_row = len(_call_with_retry(sheet.get_all_values)) + 1
    end_row = start_row + len(values) - 1
    if sheet.row_count < end_row:
        _call_with_retry(sheet.add_rows, end_row - sheet.row_count)
    _call_with_retry(sheet.update, f"A{start_row}", values, value_input_option="USER_ENTERED")


def sort_detail_and_log() -> None:
    """「取引明細」「ログ」を、日時（B列）で降順（新しい→古い）に並べ替える
    （GASのsortDetailSheetByDate / sortLogSheetByDateに相当。Python移植時に
    この並べ替え処理自体が抜けており、メールを取得した順＝Gmail受信順のまま
    シートに残ってしまい、実際の利用日と行の並びが一致しない不具合があった）。
    ヘッダー行（1〜2行目）は対象外にする。"""
    ss = _spreadsheet()

    detail_sheet = ss.worksheet(config.SHEET_DETAIL)
    detail_last_row = len(_call_with_retry(detail_sheet.get_all_values))
    if detail_last_row > 2:
        _call_with_retry(detail_sheet.sort, (2, "des"), range=f"A3:E{detail_last_row}")

    log_sheet = ss.worksheet(config.SHEET_LOG)
    log_last_row = len(_call_with_retry(log_sheet.get_all_values))
    if log_last_row > 2:
        _call_with_retry(log_sheet.sort, (2, "des"), range=f"A3:C{log_last_row}")


def _border_request(sheet, start_row: int, end_row: int, start_col: int, end_col: int) -> dict | None:
    """指定範囲（1始まりの行・列番号、両端含む）に全罫線を引くリクエストを組み立てる（送信はしない）。"""
    if end_row < start_row:
        return None
    border_style = {"style": "SOLID", "width": 1, "color": {"red": 0, "green": 0, "blue": 0}}
    return {
        "updateBorders": {
            "range": {
                "sheetId": sheet.id,
                "startRowIndex": start_row - 1,
                "endRowIndex": end_row,
                "startColumnIndex": start_col - 1,
                "endColumnIndex": end_col,
            },
            "top": border_style,
            "bottom": border_style,
            "left": border_style,
            "right": border_style,
            "innerHorizontal": border_style,
            "innerVertical": border_style,
        }
    }


def _number_format_request(sheet, start_row: int, end_row: int, col: int, pattern: str = "#,##0") -> dict | None:
    """
    指定した1列（1始まりの列番号）の指定行範囲に、桁区切り（カンマ）付きの数値書式を
    設定するリクエストを組み立てる（送信はしない）。

    ■ 2026/09/17 追加
    「日次利用額」「予算計画」の金額欄には元々カンマ区切りの表示形式が設定されて
    いたが、これはスプレッドシート側で手動で設定されていたものであり、コード側で
    明示的に設定している箇所はどこにも無かった。「取引明細」「ログ」にはこの手動設定が
    されていなかったため、金額が桁区切り無しの数字のまま表示されていた。
    罫線（_apply_borders相当）と同じタイミングでこの書式リクエストも適用することで、
    シートを手動でいじらなくても、データが増えるたびに自動でカンマ区切りが
    反映されるようにした。
    """
    if end_row < start_row:
        return None
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet.id,
                "startRowIndex": start_row - 1,
                "endRowIndex": end_row,
                "startColumnIndex": col - 1,
                "endColumnIndex": col,
            },
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": pattern}}},
            "fields": "userEnteredFormat.numberFormat",
        }
    }


def _apply_borders(sheet, start_row: int, end_row: int, start_col: int, end_col: int) -> None:
    """指定範囲（1始まりの行・列番号、両端含む）に全罫線を引く。"""
    request = _border_request(sheet, start_row, end_row, start_col, end_col)
    if request is None:
        return
    _call_with_retry(sheet.spreadsheet.batch_update, {"requests": [request]})


def apply_detail_sheet_borders() -> None:
    """「取引明細」のB2:E(最終行)に全罫線を引き、金額列（D列）にカンマ区切りの数値書式を
    設定する（GASのapplyDetailSheetBordersに相当）。
    データが増えるたびに呼び出すことで、常に最終行まで罫線・書式が自動で伸びるようにする。"""
    sheet = _spreadsheet().worksheet(config.SHEET_DETAIL)
    last_row = len(_call_with_retry(sheet.get_all_values))
    if last_row < 2:
        return
    requests = [_border_request(sheet, start_row=2, end_row=last_row, start_col=2, end_col=5)]  # B〜E列
    if last_row >= 3:  # 3行目以降がデータ（1行目は空欄、2行目はヘッダー）
        requests.append(_number_format_request(sheet, start_row=3, end_row=last_row, col=4))  # D列（金額）
    _call_with_retry(sheet.spreadsheet.batch_update, {"requests": requests})


def apply_log_sheet_borders() -> None:
    """「ログ」のB2:C(最終行)に全罫線を引き、金額列（C列）にカンマ区切りの数値書式を
    設定する（GASのapplyLogSheetBordersに相当）。"""
    sheet = _spreadsheet().worksheet(config.SHEET_LOG)
    last_row = len(_call_with_retry(sheet.get_all_values))
    if last_row < 2:
        return
    requests = [_border_request(sheet, start_row=2, end_row=last_row, start_col=2, end_col=3)]  # B〜C列
    if last_row >= 3:  # 3行目以降がデータ（1行目は空欄、2行目はヘッダー）
        requests.append(_number_format_request(sheet, start_row=3, end_row=last_row, col=3))  # C列（金額）
    _call_with_retry(sheet.spreadsheet.batch_update, {"requests": requests})


def get_detail_worksheet():
    """「取引明細」のworksheetオブジェクトをそのまま返す（行番号を指定した直接操作が
    必要な軽量ツール向け）。"""
    return _spreadsheet().worksheet(config.SHEET_DETAIL)


def get_sender_configs() -> list[dict]:
    """
    「送信元リスト」シートからカード会社ごとの設定を読み込む（GASのgetSenderConfigsに相当。
    Python移植時にこのシートの読み込み自体が抜けており、常にconfig.pyにハードコードされた
    SENDER_LISTだけが使われていた）。シートが無い、またはデータが1件も無い場合は
    config.SENDER_LIST（デフォルト）を使う。
    シート構成：A列空欄、B列=サービス名（表示用）、C列=送信元メールアドレス、
    D列=除外キーワード（件名、カンマ区切り、任意）、E列=金額の目印文言（任意）。
    """
    try:
        sheet = _spreadsheet().worksheet(config.SHEET_SENDER_LIST)
    except gspread.exceptions.WorksheetNotFound:
        return config.SENDER_LIST

    records = _call_with_retry(sheet.get_all_values)
    configs = []
    for row in records[2:]:  # 3行目以降がデータ
        address = str(row[2]).strip() if len(row) > 2 else ""
        if not address:
            continue
        exclude_raw = row[3] if len(row) > 3 else ""
        exclude_keywords = (
            [s.strip() for s in str(exclude_raw).split(",") if s.strip()] if exclude_raw else []
        )
        amount_keyword_raw = row[4] if len(row) > 4 else ""
        amount_keyword = str(amount_keyword_raw).strip() or None
        configs.append({
            "address": address,
            "exclude_subject_keywords": exclude_keywords,
            "amount_keyword": amount_keyword,
        })
    return configs if configs else config.SENDER_LIST


def _row_matches_header(row: list, expected: list[str]) -> bool:
    """B列（インデックス1）以降が、期待するヘッダー文言と一致しているか確認する。"""
    for i, exp in enumerate(expected, start=1):
        if i >= len(row) or row[i] != exp:
            return False
    return True


def ensure_detail_and_log_headers() -> None:
    """
    「取引明細」「ログ」の1〜2行目（1行目は空欄、2行目がヘッダー）が無ければ、
    先頭に挿入して復元する（自己修復）。

    以前、書き込み方法の不具合でヘッダー行ごと上書きされ、1行目からいきなり
    データが始まってしまう事故が起きたことがあった。この状態のままだと、
    sort_detail_and_log・apply_detail_sheet_borders等は「3行目以降がデータ」という
    前提で動くため、1行目（本来ヘッダーがあるべき場所に紛れ込んだデータ）だけが
    並べ替え・罫線の対象から外れ、常に最上部に取り残されてしまう。
    書き込み系の処理（clear_detail_and_log等）の直前に必ず呼び、この状態を防ぐ。
    """
    ss = _spreadsheet()

    detail_sheet = ss.worksheet(config.SHEET_DETAIL)
    detail_values = _call_with_retry(detail_sheet.get_all_values)
    detail_header_ok = len(detail_values) >= 2 and _row_matches_header(
        detail_values[1], ["日時", "店舗名", "金額", "カテゴリ"]
    )
    if not detail_header_ok:
        print("⚠️ 「取引明細」のヘッダー行が見つからなかったため、先頭に挿入し直しました。")
        _call_with_retry(
            detail_sheet.insert_rows,
            [["", "", "", "", ""], ["", "日時", "店舗名", "金額", "カテゴリ"]],
            row=1, value_input_option="USER_ENTERED",
        )

    log_sheet = ss.worksheet(config.SHEET_LOG)
    log_values = _call_with_retry(log_sheet.get_all_values)
    log_header_ok = len(log_values) >= 2 and _row_matches_header(log_values[1], ["日時", "金額"])
    if not log_header_ok:
        print("⚠️ 「ログ」のヘッダー行が見つからなかったため、先頭に挿入し直しました。")
        _call_with_retry(
            log_sheet.insert_rows,
            [["", "", ""], ["", "日時", "金額"]],
            row=1, value_input_option="USER_ENTERED",
        )


def clear_detail_and_log() -> None:
    """「取引明細」「ログ」の中身（ヘッダーを除く）をすべて削除する。
    全期間の再構築（rebuild_all_transactions.py）の前処理として使う。
    削除の前に、ヘッダー行が欠けていないか必ず確認・復元する。"""
    ensure_detail_and_log_headers()

    detail_sheet = _spreadsheet().worksheet(config.SHEET_DETAIL)
    detail_last_row = len(_call_with_retry(detail_sheet.get_all_values))
    if detail_last_row >= 3:
        _call_with_retry(detail_sheet.delete_rows, 3, detail_last_row)

    log_sheet = _spreadsheet().worksheet(config.SHEET_LOG)
    log_last_row = len(_call_with_retry(log_sheet.get_all_values))
    if log_last_row >= 3:
        _call_with_retry(log_sheet.delete_rows, 3, log_last_row)


# ============================================================
# システム状態（実行のたびに重複送信しないための記録）
# ============================================================

_STATE_CELLS = {
    "daily_report": "B1",
    "weekly_report": "B2",
    "subscription_report": "B3",
    "health_check": "B4",
    "annual_summary": "B5",
    "review_comment": "B6",
    "unclassified_report": "B7",
    "near_duplicate_warned": "B8",
    "error_notified": "B9",
}


def _get_or_create_state_sheet():
    ss = _spreadsheet()
    try:
        return ss.worksheet("システム状態")
    except Exception:
        sheet = _call_with_retry(ss.add_worksheet, title="システム状態", rows=10, cols=5)
        _call_with_retry(sheet.update_acell, "A1", "最終送信日")
        return sheet


def get_state(key: str) -> str:
    """システム状態シートから、指定キーの最終送信日（などの記録値）を取得する。"""
    sheet = _get_or_create_state_sheet()
    cell = _STATE_CELLS[key]
    return _call_with_retry(sheet.acell, cell).value or ""


def set_state(key: str, value: str) -> None:
    """システム状態シートに、指定キーの記録値を書き込む。"""
    sheet = _get_or_create_state_sheet()
    cell = _STATE_CELLS[key]
    _call_with_retry(sheet.update_acell, cell, value)


def get_last_daily_report_date() -> str:
    """「システム状態」シートから、最後に日次決算を送った日付(yyyy/MM/dd)を取得する。無ければ空文字。"""
    return get_state("daily_report")


def set_last_daily_report_date(date_str: str) -> None:
    """「システム状態」シートに、最後に日次決算を送った日付(yyyy/MM/dd)を書き込む。"""
    set_state("daily_report", date_str)