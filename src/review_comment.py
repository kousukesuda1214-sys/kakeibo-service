"""
月次振り返りコメント：締め日に、その日締まった期間のカテゴリ別内訳・よく使った店舗を
まとめ、一言コメントを添えてLINEに送る。
ANTHROPIC_API_KEYが設定されていればClaude APIで自然文を生成し、未設定ならテンプレートで代替する。
GAS版のsendClosingPeriodReviewComment / generateMonthlyReviewCommentを移植。

■ 雛形化での変更点
締め期間（旧：16日〜15日固定）・締め日の判定を periods.py 経由にし、CLOSING_DAY に追従させた。
"""

from datetime import datetime

import requests

from . import config, sheets_client, line_client
from .periods import in_report_window, is_closing_day, period_for_closing_month


def _build_fallback_comment(stats: dict) -> str:
    if not stats["top_categories"]:
        return f"{stats['period_label']}は記録できた取引がありませんでした。"
    top_name, top_amount = stats["top_categories"][0]
    top_percent = round(top_amount / stats["period_total"] * 100) if stats["period_total"] > 0 else 0
    comment = f"{stats['period_label']}は「{top_name}」が全体の{top_percent}%（{top_amount:,}円）を占める期間でした。"
    if stats["top_merchants"]:
        comment += f"よく利用したのは「{stats['top_merchants'][0][0]}」でした。"
    return comment


def _call_claude(stats: dict) -> str | None:
    top_category_text = "、".join(f"{name}{amount:,}円" for name, amount in stats["top_categories"])
    top_merchant_text = "、".join(f"{name}{amount:,}円" for name, amount in stats["top_merchants"])

    prompt = (
        f"以下は家計簿システムの{stats['period_label']}（締め期間）の集計データです。この人に向けて、"
        "気軽に読める2〜3文の日本語の振り返りコメントを書いてください。説教くさくならず、"
        "軽い気づきや労いを含めてください。コメント本文だけを出力してください。\n\n"
        f"合計利用額：{stats['period_total']:,}円（{stats['transaction_count']}件）\n"
        f"カテゴリ別上位：{top_category_text}\n"
        f"店舗別上位：{top_merchant_text}"
    )

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": config.ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 300,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"Claude APIの応答が異常です（コード: {resp.status_code}）: {resp.text}")
        return None
    data = resp.json()
    for block in data.get("content", []):
        if block.get("type") == "text":
            return block["text"].strip()
    return None


def _generate_comment(stats: dict) -> str:
    if config.ANTHROPIC_API_KEY:
        try:
            comment = _call_claude(stats)
            if comment:
                return comment
        except Exception as e:
            print(f"AIコメント生成に失敗したため、簡易コメントで代替します: {e}")
    return _build_fallback_comment(stats)


def _base_name(merchant: str) -> str:
    idx = merchant.find("（")
    return (merchant[:idx] if idx != -1 else merchant).strip()


def build_review_for_period(period_start: datetime, period_end: datetime) -> str | None:
    category_totals: dict[str, int] = {}
    merchant_totals: dict[str, int] = {}
    period_total = 0
    transaction_count = 0

    for r in sheets_client.get_parsed_detail_rows():
        if not (period_start <= r["date"] <= period_end):
            continue
        category_totals[r["category"]] = category_totals.get(r["category"], 0) + r["amount"]
        period_total += r["amount"]
        transaction_count += 1
        name = _base_name(r["merchant"])
        if name:
            merchant_totals[name] = merchant_totals.get(name, 0) + r["amount"]

    if transaction_count == 0:
        return None

    sorted_categories = sorted(category_totals.items(), key=lambda kv: kv[1], reverse=True)
    sorted_merchants = sorted(merchant_totals.items(), key=lambda kv: kv[1], reverse=True)[:3]
    period_label = (
        f"{period_start.month}/{period_start.day}〜{period_end.month}/{period_end.day}"
        f"（{period_end.month}月{period_end.day}日締め）"
    )

    stats = {
        "period_label": period_label,
        "period_total": period_total,
        "transaction_count": transaction_count,
        "top_categories": sorted_categories[:3],
        "top_merchants": sorted_merchants,
    }

    message = f"🗒️ {period_label}の振り返り\n\n合計：{period_total:,}円（{transaction_count}件）\n\n【カテゴリ別】\n"
    for name, amount in sorted_categories[:5]:
        percent = round(amount / period_total * 100) if period_total > 0 else 0
        message += f"{name}：{amount:,}円（{percent}%）\n"

    message += f"\n{_generate_comment(stats)}"
    return message


def maybe_send_review_comment(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    if not is_closing_day(now):
        return False
    if not in_report_window(now):
        return False
    if sheets_client.get_state("review_comment") == now.strftime("%Y/%m/%d"):
        return False

    period_start, period_end = period_for_closing_month(now.year, now.month)
    message = build_review_for_period(period_start, period_end)
    sheets_client.set_state("review_comment", now.strftime("%Y/%m/%d"))

    if message is None:
        print("振り返りコメント：対象期間のデータが無いため送信しませんでした")
        return False

    line_client.send_line_message(message)
    print("月次振り返りコメントを送信しました")
    return True
