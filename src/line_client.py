"""
LINE Messaging APIへのメッセージ送信。GASのsendLineMessageTo()を移植。
"""

import requests

from . import config


class LineSendError(Exception):
    pass


def send_line_message(text: str, user_id: str | None = None) -> None:
    """
    LINEにメッセージを送信する。user_idを省略するとconfig.LINE_USER_IDに送る。
    エラー時は例外を投げる（「エラーを無視して先に進む」と原因が分からなくなるため、
    最初から失敗が分かるようにしている）。
    """
    target = user_id or config.LINE_USER_ID
    if not config.LINE_CHANNEL_ACCESS_TOKEN or not target:
        raise LineSendError(
            "LINE_CHANNEL_ACCESS_TOKEN または LINE_USER_ID が設定されていません。"
            ".env（ローカル実行時）またはGitHub Secrets（自動実行時）を確認してください。"
        )

    resp = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={
            "Authorization": f"Bearer {config.LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
        json={"to": target, "messages": [{"type": "text", "text": text}]},
        timeout=15,
    )

    if resp.status_code == 429 and "monthly limit" in resp.text:
        # 月の送信数の上限。家計簿サービスでは全員で1つのLINE公式アカウントを使うため、
        # エラーとして扱うと全員分のエラー通知が発生してしまう。送信をあきらめて先に進む（翌月1日に回復）
        print("LINEの月間送信数の上限に達しているため、このメッセージは送れませんでした")
        return

    if resp.status_code != 200:
        raise LineSendError(
            f"LINEへのメッセージ送信に失敗しました（ステータス: {resp.status_code}）: {resp.text}\n"
            "よくある原因：トークンが無効・期限切れ、LINE_USER_IDの間違い、月間メッセージ通数の上限到達など"
        )
