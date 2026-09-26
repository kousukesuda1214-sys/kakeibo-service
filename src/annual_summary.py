"""
年間サマリー：12/31に、月×カテゴリのクロス集計表を「年間サマリー」シートに作成・更新する。
GAS版のbuildAnnualSummarySheet呼び出し部分を移植。

■ 雛形化での変更点
旧版は12/31の「最初の実行」（深夜0時台）で作っていたため、12/31当日の利用が
反映されなかった。日次決算と同じ送信時間帯に作るよう変更した。
"""

from datetime import datetime

from . import sheets_client, line_client
from .periods import in_report_window


def maybe_build_annual_summary(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    if not (now.month == 12 and now.day == 31):
        return False
    if not in_report_window(now):
        return False
    if sheets_client.get_state("annual_summary") == str(now.year):
        return False

    result = sheets_client.build_annual_summary_sheet()
    sheets_client.set_state("annual_summary", str(now.year))
    line_client.send_line_message(
        "✅ 年間サマリーを作成・更新しました。\n\n"
        f"対象月数：{result['months']}か月分\n"
        f"カテゴリ数：{result['categories']}種類"
    )
    print("年間サマリーを作成・更新しました")
    return True
