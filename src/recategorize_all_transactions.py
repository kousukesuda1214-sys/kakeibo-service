"""
【便利ツール】Gmailを読み直さずに、「取引明細」に記録済みの全行を、今の
「カテゴリルール」シート（＋AIによる自動判定）で一括して分類し直す。
「カテゴリルール」シートにキーワードを追加・変更したあと、過去の取引にも
反映させたいときに使う。店舗名・金額・日時は一切変更しない（カテゴリ列だけを上書きする）。

GAS版のrecategorizeAllTransactionsに相当。全期間のGmail再読み込み
（rebuild_all_transactions.py）より、はるかに速く終わる。

■ 実行方法（自分のパソコンで実行する）
    python -m src.recategorize_all_transactions

■ 雛形化での変更点
変更のあった行を1行ずつ書き込むと、件数が多いときにGoogle Sheets APIの
書き込み回数制限（1分あたり60回）に引っかかって途中で止まってしまうため、
1回の呼び出しでまとめて書き込むようにした。
"""

from . import config, parser, sheets_client


def main() -> None:
    sheet = sheets_client.get_detail_worksheet()
    records = sheets_client._call_with_retry(sheet.get_all_values)

    updates: list[tuple[int, str, str]] = []  # (行番号, 変更前, 変更後)
    checked_count = 0

    for i, row in enumerate(records):
        if i < 2:  # 1〜2行目はヘッダー
            continue
        if len(row) < 3:
            continue
        merchant = row[2]
        current_category = row[4] if len(row) > 4 else ""
        if not merchant:
            continue

        checked_count += 1
        new_category = parser.categorize(merchant)
        if new_category == config.DEFAULT_CATEGORY:
            new_category = parser.ai_categorize(merchant)

        if new_category != current_category:
            updates.append((i + 1, current_category, new_category))

    if updates:
        sheets_client._call_with_retry(
            sheet.batch_update,
            [{"range": f"E{row_number}", "values": [[new]]} for row_number, _, new in updates],
        )

    print("✅ 再分類が完了しました。")
    print(f"チェックした件数：{checked_count}件")
    print(f"カテゴリが変わった件数：{len(updates)}件")
    for row_number, old, new in updates[:20]:
        print(f"  {row_number}行目：{old or '（空欄）'} → {new}")
    if len(updates) > 20:
        print(f"  ...他{len(updates) - 20}件")
    print("店舗名・金額・日時は変更していません。カテゴリ（E列）だけを更新しました。")


if __name__ == "__main__":
    main()
