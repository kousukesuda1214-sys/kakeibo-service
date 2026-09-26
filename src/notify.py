"""
エラー発生時に、LINEとGmail両方へ通知する。GAS版のnotifyErrorを移植したもの。

Gmail通知には、ANTHROPIC_API_KEYが設定されていれば、Claude（Haiku）による簡単な
原因診断・修正の方向性を添える。LINEは短いまま（詳しい内容はメールで確認する想定）。
Claude呼び出し自体が失敗しても、元々のエラー通知（LINE・メールとも）は必ず届く
（診断コメントはあくまで「あれば助かるおまけ」で、通知の成否には一切影響させない）。

■ 雛形化での変更点：同じエラーの通知は1日1回まで
30分おきの自動実行で同じエラーが起き続けると、そのたびにLINEが送られ、
LINEの無料枠（月200通）を数日で使い切ってしまう（kakeibo-pythonで実際に起きた。
Googleのトークン切れで15分おきにエラー通知が飛び、月の上限に達して、
日次決算も含めてLINEが一切届かなくなった）。
- 「どこで・何の種類のエラーが起きたか」が同じものは、1日1回だけ通知する
  （記録は「システム状態」シートに残す）
- Googleの認証切れなどでシート自体が読めない場合は、記録ができないため、
  日次決算を送る時間帯（DAILY_REPORT_HOUR時台）だけ通知する
  （その時間帯の実行は30分おきで最大2回なので、1日最大2通に収まる）
"""

import json
import os
import traceback
from datetime import datetime

import requests

from . import config, line_client, gmail_client, sheets_client

SYSTEM_LABEL = "【家計簿システム】"


def _diagnose_with_claude(where: str, traceback_text: str) -> str | None:
    """
    エラーのトレースバックをClaudeに渡し、考えられる原因・直すならどこを見るべきかを
    短くまとめてもらう。ANTHROPIC_API_KEYが未設定、またはAPI呼び出しに失敗した場合は
    Noneを返す（呼び出し側は診断コメント無しでいつも通り通知する）。
    """
    if not config.ANTHROPIC_API_KEY:
        return None

    prompt = (
        f"以下は家計簿システム（Python、Gmail解析→スプレッドシート記録→LINE通知）の"
        f"「{where}」処理中に発生したエラーのトレースバックです。\n\n"
        f"{traceback_text}\n\n"
        "この内容から考えられる原因と、直すならどのあたりを見るべきかを、"
        "日本語で3〜5行程度で簡潔にまとめてください。断定できない場合は"
        "「〜の可能性があります」のように推測であることが分かる書き方にしてください。"
        "このシステムの利用者はプログラミングに詳しくない可能性があるため、"
        "設定（.env・GitHub Secrets・スプレッドシート）の見直しで直る可能性がある場合は"
        "それを優先して書いてください。診断コメント本文だけを出力してください。"
    )

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": config.ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5",
                "max_tokens": 400,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"Claudeによるエラー診断に失敗しました（コード: {resp.status_code}）: {resp.text}")
            return None
        data = resp.json()
        for block in data.get("content", []):
            if block.get("type") == "text":
                return block["text"].strip()
    except requests.RequestException as e:
        print(f"Claudeによるエラー診断の呼び出しに失敗しました: {e}")
    return None


def _report_to_runner(where: str, error: Exception) -> None:
    """
    サービス版（runner.py から利用者ごとに実行される場合）のエラー報告。
    エラーを直せるのは運営者だけなので、利用者のLINE・メールには送らない。
    また、GitHub Actionsの実行ログは公開されるため、エラーの「場所」と「種類」だけを伝え、
    エラーの本文（メールの件名や金額が含まれることがある）は出さない。
    """
    where_short = where.split("（")[0]
    print(f"KAKEIBO_ERROR|{where_short}|{type(error).__name__}")
    for frame in traceback.extract_tb(error.__traceback__):
        print(f"KAKEIBO_TRACE|{os.path.basename(frame.filename)}:{frame.lineno} {frame.name}")


def _should_notify(where: str, error: Exception, now: datetime) -> bool:
    """このエラーを今通知すべきかを判定し、通知する場合は「通知済み」として記録する。"""
    signature = f"{where}|{type(error).__name__}"
    today = now.strftime("%Y/%m/%d")

    try:
        raw = sheets_client.get_state("error_notified")
        record = json.loads(raw) if raw else {}
        if not isinstance(record, dict):
            record = {}
    except Exception as state_error:
        # シートが読めない（認証切れなど）→ 記録できないので、決まった時間帯だけ通知する
        print(f"エラー通知の記録を読めなかったため、{config.DAILY_REPORT_HOUR}時台だけ通知します: {state_error}")
        return now.hour == config.DAILY_REPORT_HOUR

    if record.get(signature) == today:
        return False

    # 今日以外の古い記録は捨てて、今回の分を記録する
    record = {key: day for key, day in record.items() if day == today}
    record[signature] = today
    try:
        sheets_client.set_state("error_notified", json.dumps(record, ensure_ascii=False))
    except Exception as state_error:
        print(f"エラー通知の記録に失敗しました（通知はそのまま行います）: {state_error}")
    return True


def notify_error(where: str, error: Exception) -> None:
    """
    処理中に起きたエラーを、LINEとメール両方に通知する（どちらかが失敗してももう片方は試す）。

    同じ場所・同じ種類のエラーは1日1回しか通知しない（ターミナル・GitHub Actionsの
    ログには毎回表示される）。

    【重要】notify_errorは必ずexceptブロックの中から呼ぶこと。traceback.format_exc()は
    「今処理中の例外」の情報をそこから取り出しており、exceptブロックの外で呼ぶと
    詳しいトレースバックが取れない（その場合はエラーの型と内容だけの簡易表示になる）。
    """
    if os.environ.get("KAKEIBO_SERVICE_MODE") == "1":
        _report_to_runner(where, error)
        return

    traceback_text = traceback.format_exc()
    if traceback_text.strip() == "NoneType: None":
        traceback_text = f"{type(error).__name__}: {error}"

    print(f"エラー発生: {where}\n{traceback_text}")
    if not _should_notify(where, error, datetime.now()):
        print("同じエラーは本日すでに通知済みのため、LINE・メールでの通知は省略します。")
        return

    detail = f"{SYSTEM_LABEL}エラー発生: {where}\n\n{error}\n\n{traceback_text}"

    diagnosis = _diagnose_with_claude(where, traceback_text)
    if diagnosis:
        detail += f"\n\n---\n🤖 Claudeによる診断（自動生成・あくまで参考程度に）\n{diagnosis}"

    try:
        line_client.send_line_message(f"⚠️ エラー発生: {where}\n\n{error}")
    except Exception as line_error:
        print(f"LINEへのエラー通知にも失敗しました: {line_error}")

    try:
        gmail_client.send_email(f"{SYSTEM_LABEL}エラー発生: {where}", detail)
    except Exception as mail_error:
        print(f"メールでのエラー通知にも失敗しました: {mail_error}")
