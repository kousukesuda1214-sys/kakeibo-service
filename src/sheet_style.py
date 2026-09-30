"""
家計簿スプレッドシートの見た目（列の幅・金額の3桁区切り・見出しの固定・入力欄の色・シートの大きさ）を整える。

■ レイアウトの版数（LAYOUT_VERSION）
見た目の設定を変えたら、LAYOUT_VERSION を1つ上げる。すると、すでに使っている利用者の家計簿にも、
次回の自動実行で新しい見た目が反映される（「システム状態」シートに、反映済みの版数を記録している）。
アプリのアップデートで画面が新しくなるのと同じ考え方。

■ API呼び出しについて
すべての書式設定を、スプレッドシート全体で1回の呼び出し（batch_update）にまとめて送る。
"""

from . import config, sheets_client

LAYOUT_VERSION = 2  # 2：「送信元リスト」にF列（読み取る件名）を足した

# シート（タブ）の並び順。毎日見るものを左に、たまにしか触らない設定系を右にしている
# （晃介さん専用版の並び順に、サービス版で増えた「カテゴリルール」「設定」を加えたもの）。
# ここに無いシート（利用者が自分で作ったシートなど）は、この並びの後ろに、元の順番のまま置く。
SHEET_ORDER = [
    config.SHEET_BUDGET,
    getattr(config, "SHEET_DAILY_USAGE", "日次利用額"),
    config.SHEET_DETAIL,
    config.SHEET_LOG,
    getattr(config, "SHEET_ANNUAL_SUMMARY", "年間サマリー"),
    config.SHEET_CATEGORY_BUDGET,
    sheets_client.SHEET_CATEGORY_RULES,
    "システム状態",
    config.SHEET_SENDER_LIST,
    "設定",
]

MIN_COLUMNS = 26          # シートの列数の最低ライン（少ないと、右側が灰色になって使いにくい）
YEN = "#,##0"
YEN_SIGNED = "#,##0;[Red]-#,##0"
INPUT_COLOR = {"red": 0.851, "green": 0.918, "blue": 0.827}   # 入力してほしい欄の色（薄い緑）
HEADER_COLOR = {"red": 0.953, "green": 0.953, "blue": 0.953}  # 見出しの色（薄い灰色）

# シートごとの設定
#   widths    … 列ごとの幅（ピクセル）。A列から順に
#   freeze    … 上から何行を固定するか（スクロールしても見出しが見えたままになる）
#   headers   … 見出しの行（範囲）
#   numbers   … 金額の列と書式（列番号は A=1）
#   inputs    … 利用者に書いてほしい欄（色を付ける）
#   data_from … データが始まる行（数値の書式・入力欄の色を付ける範囲の開始行）
LAYOUTS = {
    "設定": {
        "widths": [30, 220, 200, 520, 340], "freeze": 2, "headers": ["B2:E2"],
        "inputs": ["C"], "data_from": 3,
    },
    config.SHEET_SENDER_LIST: {
        "widths": [30, 170, 260, 300, 180, 320], "freeze": 2, "headers": ["B2:F2"],
        "inputs": ["B", "C", "D", "E", "F"], "data_from": 3,
    },
    config.SHEET_BUDGET: {
        "widths": [30, 110, 100, 110, 100, 110, 100, 110, 120], "freeze": 3, "headers": ["B2:I3"],
        "numbers": {"C": YEN, "D": YEN, "E": YEN, "F": YEN, "G": YEN_SIGNED, "H": YEN_SIGNED, "I": YEN},
        "inputs": ["C"], "data_from": 4,
    },
    config.SHEET_DETAIL: {
        "widths": [30, 150, 440, 100, 160], "freeze": 2, "headers": ["B2:E2"],
        "numbers": {"D": YEN_SIGNED}, "data_from": 3,
    },
    config.SHEET_LOG: {
        "widths": [30, 120, 110], "freeze": 2, "headers": ["B2:C2"],
        "numbers": {"C": YEN_SIGNED}, "data_from": 3,
    },
    getattr(config, "SHEET_DAILY_USAGE", "日次利用額"): {
        "widths": [120, 110, 60, 440, 100, 160, 170], "freeze": 1, "headers": ["A1:G1"],
        "numbers": {"B": YEN_SIGNED, "E": YEN_SIGNED}, "data_from": 2,
    },
    sheets_client.SHEET_CATEGORY_RULES: {
        "widths": [30, 170, 600, 520], "freeze": 2, "headers": ["B2:C2"],
        "inputs": ["B", "C"], "data_from": 3,
    },
    config.SHEET_CATEGORY_BUDGET: {
        "widths": [30, 170, 130], "freeze": 2, "headers": ["B2:C2"],
        "numbers": {"C": YEN}, "inputs": ["C"], "data_from": 3,
    },
}


def _col_index(letter: str) -> int:
    return ord(letter) - ord("A")


def _a1_to_grid(sheet_id: int, a1: str) -> dict:
    start, end = a1.split(":")
    return {
        "sheetId": sheet_id,
        "startRowIndex": int(start[1:]) - 1,
        "endRowIndex": int(end[1:]),
        "startColumnIndex": _col_index(start[0]),
        "endColumnIndex": _col_index(end[0]) + 1,
    }


def _requests_for(ws, layout: dict) -> list[dict]:
    sheet_id = ws.id
    requests = []

    # シートの大きさ（列が少なすぎると右側が灰色になる）と、見出しの固定
    grid = {"frozenRowCount": layout["freeze"]}
    fields = "gridProperties.frozenRowCount"
    if ws.col_count < MIN_COLUMNS:
        grid["columnCount"] = MIN_COLUMNS
        fields += ",gridProperties.columnCount"
    requests.append({
        "updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "gridProperties": grid},
            "fields": fields,
        }
    })

    # 列の幅
    for index, width in enumerate(layout["widths"]):
        requests.append({
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": index, "endIndex": index + 1},
                "properties": {"pixelSize": width},
                "fields": "pixelSize",
            }
        })

    # 見出し：太字・薄い灰色
    for a1 in layout.get("headers", []):
        requests.append({
            "repeatCell": {
                "range": _a1_to_grid(sheet_id, a1),
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}, "backgroundColor": HEADER_COLOR}},
                "fields": "userEnteredFormat.textFormat.bold,userEnteredFormat.backgroundColor",
            }
        })

    # 金額の列：3桁区切り（列全体のデータ部分に設定するので、あとから増えた行にも効く）
    data_row_index = layout["data_from"] - 1
    for letter, pattern in layout.get("numbers", {}).items():
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id, "startRowIndex": data_row_index,
                    "startColumnIndex": _col_index(letter), "endColumnIndex": _col_index(letter) + 1,
                },
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": pattern}}},
                "fields": "userEnteredFormat.numberFormat",
            }
        })

    # 利用者に書いてほしい欄：薄い緑（晃介さん専用版の「予算計画」と同じ色づかい）
    # （色を付ける行数は、シートの実際の行数を超えないようにする。超えると「範囲外」エラーになる）
    input_end = min(data_row_index + 60, ws.row_count)
    for letter in layout.get("inputs", []):
        if input_end <= data_row_index:
            break
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id, "startRowIndex": data_row_index, "endRowIndex": input_end,
                    "startColumnIndex": _col_index(letter), "endColumnIndex": _col_index(letter) + 1,
                },
                "cell": {"userEnteredFormat": {"backgroundColor": INPUT_COLOR}},
                "fields": "userEnteredFormat.backgroundColor",
            }
        })
    return requests


def apply_layout() -> None:
    """今あるシートすべてに、見た目の設定をまとめて反映する（1回のAPI呼び出し）。"""
    ss = sheets_client._spreadsheet()
    worksheets = {ws.title: ws for ws in sheets_client._call_with_retry(ss.worksheets)}
    requests = []
    for title, layout in LAYOUTS.items():
        if title in worksheets:
            requests.extend(_requests_for(worksheets[title], layout))
    if requests:
        sheets_client._call_with_retry(ss.batch_update, {"requests": requests})


def ensure_sheet_order() -> bool:
    """シート（タブ）が SHEET_ORDER の順に並んでいなければ並べ替える。並べ替えたらTrue。
    「年間サマリー」のように途中で増えるシートもあるため、毎回の実行の最後に呼ぶ。
    並び順が合っていれば、読み取り1回だけで終わる。"""
    ss = sheets_client._spreadsheet()
    worksheets = sheets_client._call_with_retry(ss.worksheets)
    rank = {title: i for i, title in enumerate(SHEET_ORDER)}
    # 並び順に無いシートは後ろへ。同じ扱いのもの同士は、今の順番を保つ（sorted は安定ソート）
    desired = sorted(worksheets, key=lambda ws: rank.get(ws.title, len(SHEET_ORDER)))
    if [ws.id for ws in desired] == [ws.id for ws in worksheets]:
        return False
    requests = [
        {"updateSheetProperties": {"properties": {"sheetId": ws.id, "index": index}, "fields": "index"}}
        for index, ws in enumerate(desired)
    ]
    sheets_client._call_with_retry(ss.batch_update, {"requests": requests})
    return True


def apply_layout_if_outdated() -> bool:
    """この家計簿に、まだ最新の見た目が反映されていなければ反映する。反映したらTrue。"""
    if sheets_client.get_state("layout_version") == str(LAYOUT_VERSION):
        return False
    apply_layout()
    sheets_client.set_state("layout_version", str(LAYOUT_VERSION))
    return True
