"""
スプレッドシートの「設定」シートから、締め日・通知時刻などの設定を読み込む。
GAS版と同じように、設定の変更をスプレッドシートだけで完結させるためのもの。

■ 仕組み
- main.py などの最初に apply_sheet_settings() を呼ぶと、「設定」シートの値で
  config の値を上書きする（優先順位：設定シート ＞ 環境変数・.env ＞ config.py の初期値）
- 「設定」シートが無ければ、今の設定値（初期値）を書き込んだ状態で自動で作る
- 雛形の更新で設定項目が増えたときは、足りない行を末尾に自動で追加する
- 読み取れない値が書かれていた場合は、その項目だけ初期値のまま動かし、
  E列「状態」に理由を書き込む（LINEには送らない）
- シートが読めない（認証切れなど）場合も、止まらずに初期値のまま動く

■ シート構成
1行目は空欄、2行目がヘッダー、3行目以降が
B列=設定項目、C列=値（利用者が書き換える）、D列=説明、E列=状態（自動で書き込まれる）。
項目は「B列の名前」で探すので、行の順番を入れ替えても問題ない。
"""

import os
import re
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import gspread

from . import config, sheets_client

SHEET_SETTINGS = getattr(config, "SHEET_SETTINGS", "設定")

_WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]
_TRUE_WORDS = {"使う", "する", "はい", "on", "true", "有効", "1", "○", "◯"}
_FALSE_WORDS = {"使わない", "しない", "いいえ", "off", "false", "無効", "0", "×"}

# (configの属性名, シートでの項目名, 種類, 説明)
SETTINGS = [
    ("TZ", "タイムゾーン", "tz",
     "日本在住なら Asia/Tokyo のまま。海外在住なら America/Los_Angeles など"),
    ("CLOSING_DAY", "カードの締め日", "day",
     "毎月何日締めか（1〜31の数字）。末日締めのカードは「末日」と書く"),
    ("PAYMENT_DAY", "引落し日", "day",
     "締め日の翌月の何日に引き落とされるか（1〜31の数字）"),
    ("DAILY_REPORT_HOUR", "日次決算を送る時刻", "hour",
     "0〜23の数字。19 なら19時台に届く（最大30分ほど遅れることがある）"),
    ("DAILY_REPORT_WEEKDAYS", "日次決算を送る曜日", "weekdays_required",
     "週1〜7回、好きな曜日を選べる。「日」「月・木」「月・水・金」「平日」「毎日」など"),
    ("WEEKLY_REPORT_WEEKDAY", "週次決算を送る曜日", "weekdays",
     "「日」のように1つ、または「月・木」のように複数選べる。「毎日」「平日」「週末」「送らない」も可"),
    ("HIGH_AMOUNT_THRESHOLD", "高額利用アラートの金額", "yen",
     "この金額以上の利用があると、すぐにLINEでお知らせ"),
    ("SAMPLE_BUDGET_AMOUNT", "仮の予算", "yen",
     "「予算計画」シートにその月の予算が無いときに使う金額"),
    ("SENDER_NAME_LABEL", "通知に表示する名前", "text",
     "支払日通知などのメッセージに表示される名前"),
    ("ENABLE_PAYMENT_NOTICE", "支払日通知（三井住友カード）", "bool",
     "使う／使わない。三井住友カード以外の人は「使わない」にする"),
]


# config.py に無い（サービス版で増えた）設定項目の初期値
_DEFAULTS = {
    "DAILY_REPORT_WEEKDAYS": [1, 2, 3, 4, 5, 6, 7],  # 日次決算は毎日
}
for _attr, _default in _DEFAULTS.items():
    if not hasattr(config, _attr):
        setattr(config, _attr, list(_default))

_WEEKDAY_GROUPS = {"平日": [1, 2, 3, 4, 5], "週末": [6, 7], "土日": [6, 7]}

# ============================================================
# 値の変換（シートの文字 ⇔ configの値）
# ============================================================

def _parse(kind: str, raw: str):
    """シートに書かれた文字を、configに入れる値に変換する。読めなければ ValueError。"""
    text = str(raw).strip()
    if not text:
        raise ValueError("空欄です")
    normalized = text.translate(str.maketrans("０１２３４５６７８９", "0123456789"))

    if kind == "tz":
        if normalized in ("日本", "JST", "jst"):
            return "Asia/Tokyo"
        try:
            ZoneInfo(normalized)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("タイムゾーン名が正しくありません（例：Asia/Tokyo）")
        return normalized

    if kind == "day" and normalized in ("末日", "月末", "末"):
        return 31  # 31日が無い月は、periods.py が自動でその月の最終日として扱う

    if kind in ("day", "hour"):
        m = re.fullmatch(r"(\d{1,2})\s*(日|時)?", normalized)
        if not m:
            raise ValueError("数字で書いてください")
        value = int(m.group(1))
        low, high = (1, 31) if kind == "day" else (0, 23)
        if not low <= value <= high:
            raise ValueError(f"{low}〜{high}の数字で書いてください")
        return value

    if kind == "weekdays_required":
        # 日次決算用：週1回以上（「送らない」は選べない）
        days = _parse("weekdays", raw)
        if not days:
            raise ValueError("日次決算は週1回以上にしてください")
        return days

    if kind == "weekdays":
        # 「日」「月・木」「月 水 金」「毎日」「送らない」などを、曜日の番号のリスト（1=月〜7=日）にする
        if normalized in ("毎日", "毎日送る"):
            return [1, 2, 3, 4, 5, 6, 7]
        if normalized in ("送らない", "なし", "無し", "しない", "オフ", "off", "OFF"):
            return []
        days = set()
        for token in re.split(r"[・、,，/／\s　と]+", normalized):
            token = token.strip()
            if not token:
                continue
            if token in _WEEKDAY_GROUPS:
                days.update(_WEEKDAY_GROUPS[token])  # 「平日」「週末」「土日」
            elif token.isdigit() and 1 <= int(token) <= 7:
                days.add(int(token))
            elif token[:1] in _WEEKDAYS:
                days.add(_WEEKDAYS.index(token[:1]) + 1)  # 「木曜」「木曜日」も「木」として読む
            else:
                raise ValueError("曜日は「月・木」「平日」のように書いてください")
        if not days:
            raise ValueError("曜日を書いてください")
        return sorted(days)

    if kind == "yen":
        digits = re.sub(r"[,，円¥￥\s]", "", normalized)
        if not digits.isdigit():
            raise ValueError("金額を数字で書いてください")
        return int(digits)

    if kind == "bool":
        lowered = normalized.lower()
        if lowered in _TRUE_WORDS:
            return True
        if lowered in _FALSE_WORDS:
            return False
        raise ValueError("「使う」か「使わない」で書いてください")

    return text  # text


def _display(kind: str, value) -> str:
    """configの値を、シートに書く文字に変換する（シートを新しく作るとき用）。"""
    if kind in ("weekdays", "weekdays_required"):
        days = value if isinstance(value, (list, tuple)) else [value]
        try:
            days = sorted({int(d) for d in days})
        except (TypeError, ValueError):
            return "日"
        if not days:
            return "送らない"
        if len(days) == 7:
            return "毎日"
        if days == [1, 2, 3, 4, 5]:
            return "平日"
        if days == [6, 7]:
            return "週末"
        return "・".join(_WEEKDAYS[d - 1] for d in days if 1 <= d <= 7)
    if kind == "bool":
        if isinstance(value, str):
            value = value.strip().lower() not in ("false", "0", "no", "off", "")
        return "使う" if value else "使わない"
    return str(value)


def _current_value(attr: str):
    if attr == "TZ":
        return os.environ.get("TZ") or "Asia/Tokyo"
    return getattr(config, attr, _DEFAULTS.get(attr, ""))


# ============================================================
# シートの読み書き
# ============================================================

def ensure_settings_sheet():
    """「設定」シートが無ければ作る。あれば、足りない項目の行を末尾に追加する。"""
    ss = sheets_client._spreadsheet()
    try:
        sheet = ss.worksheet(SHEET_SETTINGS)
    except gspread.exceptions.WorksheetNotFound:
        sheet = sheets_client._call_with_retry(
            ss.add_worksheet, title=SHEET_SETTINGS, rows=max(len(SETTINGS) + 10, 20), cols=6
        )
        sheets_client._call_with_retry(
            sheet.update, "B2:E2", [["設定項目", "値", "説明", "状態（自動で表示されます）"]]
        )
        sheets_client._call_with_retry(sheet.format, "B2:E2", {"textFormat": {"bold": True}})
        print(f"「{SHEET_SETTINGS}」シートを作成しました。")

    values = sheets_client._call_with_retry(sheet.get_all_values)
    existing = {str(row[1]).strip() for row in values[2:] if len(row) > 1}

    # 雛形の更新で説明文が変わった項目は、D列を新しい説明に書き換える（C列の値には触らない）
    descriptions = {label: description for _, label, _, description in SETTINGS}
    stale = [
        {"range": f"D{index + 1}", "values": [[descriptions[str(row[1]).strip()]]]}
        for index, row in enumerate(values)
        if index >= 2 and len(row) > 1 and str(row[1]).strip() in descriptions
        and (row[3] if len(row) > 3 else "") != descriptions[str(row[1]).strip()]
    ]
    if stale:
        sheets_client._call_with_retry(sheet.batch_update, stale)
    # 足りない項目（雛形の更新で増えた項目）を、SETTINGS の並び順どおりの位置に差し込む。
    # 例：「日次決算を送る曜日」は「日次決算を送る時刻」のすぐ下に入る。
    # 上の行の書式（入力欄の緑色・枠線）を引き継ぐので、見た目も揃う。
    row_of = {str(row[1]).strip(): index + 1 for index, row in enumerate(values) if index >= 2 and len(row) > 1 and str(row[1]).strip()}

    if not row_of:
        # 新しく作ったばかりのシート：全部の行をまとめて書き込み、枠線を引く
        rows = [["", label, _display(kind, _current_value(attr)), description, ""] for attr, label, kind, description in SETTINGS]
        end_row = 2 + len(rows)
        if sheet.row_count < end_row:
            sheets_client._call_with_retry(sheet.add_rows, end_row - sheet.row_count)
        sheets_client._call_with_retry(sheet.update, f"A3:E{end_row}", rows, value_input_option="RAW")
        sheets_client._apply_borders(sheet, start_row=2, end_row=end_row, start_col=2, end_col=5)
        return sheet

    previous_row = 2  # 見出しの行
    for attr, label, kind, description in SETTINGS:
        if label in row_of:
            previous_row = row_of[label]
            continue
        insert_at = previous_row + 1
        sheets_client._call_with_retry(
            sheet.insert_row,
            ["", label, _display(kind, _current_value(attr)), description, ""],
            index=insert_at, value_input_option="RAW", inherit_from_before=True,
        )
        # 差し込んだ行より下の行番号は1つずつずれる
        row_of = {k: (v + 1 if v >= insert_at else v) for k, v in row_of.items()}
        row_of[label] = insert_at
        previous_row = insert_at
    return sheet


def _apply_timezone(tz: str) -> None:
    """プロセスのタイムゾーンを切り替える（datetime.now() などに反映される）。"""
    if os.environ.get("TZ") == tz:
        return
    os.environ["TZ"] = tz
    if hasattr(time, "tzset"):
        time.tzset()


def apply_sheet_settings() -> None:
    """
    「設定」シートの値で config を上書きする。どんなエラーが起きても例外は外に出さない
    （設定が読めないせいで家計簿の記録そのものが止まるのを防ぐため）。
    """
    try:
        sheet = ensure_settings_sheet()
        values = sheets_client._call_with_retry(sheet.get_all_values)
    except Exception as e:
        print(f"「{SHEET_SETTINGS}」シートを読めなかったため、初期設定のまま動かします: {e}")
        return

    rows_by_label = {}
    for index, row in enumerate(values):
        if index < 2 or len(row) < 2:
            continue
        label = str(row[1]).strip()
        if label:
            rows_by_label[label] = (index + 1, row)

    status_updates = []
    for attr, label, kind, _description in SETTINGS:
        if label not in rows_by_label:
            continue
        row_number, row = rows_by_label[label]
        raw = row[2] if len(row) > 2 else ""
        current_status = row[4] if len(row) > 4 else ""

        try:
            value = _parse(kind, raw)
        except ValueError as e:
            fallback = _display(kind, _current_value(attr))
            status = f"⚠️ {e}。初期値（{fallback}）で動いています"
            print(f"設定「{label}」を読み取れませんでした（{raw!r}）: {e}")
        else:
            if attr == "TZ":
                _apply_timezone(value)
            else:
                setattr(config, attr, value)
            status = "✅ 反映中"

        if status != current_status:
            status_updates.append({"range": f"E{row_number}", "values": [[status]]})

    if status_updates:
        try:
            sheets_client._call_with_retry(sheet.batch_update, status_updates)
        except Exception as e:
            print(f"設定シートの状態欄の更新に失敗しました（動作には影響しません）: {e}")


if __name__ == "__main__":
    # python -m src.settings で、設定シートの作成と読み込み結果の確認ができる
    apply_sheet_settings()
    print("現在の設定：")
    for attr, label, kind, _ in SETTINGS:
        print(f"  {label}：{_display(kind, _current_value(attr))}")
