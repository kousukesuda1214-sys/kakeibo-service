"""
Gmail APIを使って、カード利用通知メールを検索・取得するモジュール。
Gmailとスプレッドシート両方の認証（OAuth）もここで行う。

■ 認証の仕組み（雛形版）
- gmail_client_secret.json … 雛形の配布者が発行した「みんなで共有するOAuthクライアント」。
  配布者から直接（LINEやメールで）受け取り、自分のパソコンのリポジトリのフォルダに置く
  （自分でGoogle Cloudのプロジェクトを作る必要はない）。
  GitHubには上げない（.gitignoreで除外済み）。公開するとGoogleに無効化されるおそれがあるため。
  このファイルが必要なのは、自分のパソコンで初回の許可をするときだけ。
  GitHub Actionsでの自動実行では、gmail_token.json の中に必要な情報がすべて
  含まれているため不要。
- gmail_token.json … 自分のGoogleアカウントで「許可」した結果（＝自分だけのセーブデータ）。
  初回に python -m src.initial_setup を実行すると、ブラウザが開いてGoogleへのログインと
  許可を求められ、許可すると自動で作られる。
  このファイルは秘密情報なので、他人に渡さない・GitHubに直接コミットしないこと
  （自動実行用にはGitHub Secretsに登録する）。
- 許可画面で「Googleはこのアプリを確認していません」と表示されるが、これは配布者が
  Googleの有料審査を受けていないためで、中身に問題があるわけではない。
  「詳細」→「（アプリ名）に移動」で進めてよい。

■ 文字コードについて（kakeibo-python時代の経緯）
Gmail APIはformat=fullで取得する際、本文をサーバー側で自動的にUTF-8へ変換して返す。
「メッセージのソースを表示」で見える生データ（ISO-2022-JP等）とは別物なので、
charsetを見て再デコードしてはいけない（二重変換で文字化けする）。UTF-8決め打ちでよい。
"""

import base64
import os
import time
from typing import Iterator

from email.mime.text import MIMEText

import googleapiclient.errors
import requests
import urllib3.exceptions
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from . import config

# Gmail（検索・本文取得・ラベル付け）とスプレッドシート（読み書き）を
# 1つのOAuth認証にまとめている。
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/spreadsheets",
]
PROCESSED_LABEL_NAME = "日次決算_処理済み"  # サービス版では user_run.py で別の名前に切り替える


def _process_after_filter() -> str:
    """PROCESS_AFTER（yyyy/mm/dd）が指定されていれば、それ以降のメールだけに絞る検索条件を返す。
    サービス版で、新しい利用者の過去のメールを何年分も処理して時間切れになるのを防ぐため。"""
    after = os.environ.get("PROCESS_AFTER", "").strip()
    return f" after:{after}" if after else ""

# 一時的なエラーとみなし、自動リトライの対象にする例外。
# ・googleapiclient.errors.HttpError: Gmail側のクォータ制限（429/403）・一時的な5xxエラー
# ・requests.exceptions.SSLError / ConnectionError / Timeout: Wi-Fi瞬断やVPNなどによる通信断
# ・urllib3.exceptions.MaxRetryError: urllib3自体のリトライも尽きた場合
_RETRYABLE_EXCEPTIONS = (
    googleapiclient.errors.HttpError,
    requests.exceptions.SSLError,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    urllib3.exceptions.MaxRetryError,
)


def _call_with_retry(func, *args, max_attempts: int = 5, **kwargs):
    """
    Gmail APIへの呼び出しを、一時的な通信エラー・クォータ制限に対する自動リトライ付きで
    実行する。2, 4, 8, 16秒...と間隔を空けながら最大max_attempts回まで試す。

    ■ 2026/09/18 追加
    fetch_unprocessed_messages()の内部（list/get/modify呼び出し）で例外が起きると、
    for msg in fetch_unprocessed_messages(...) のループ全体がその場で止まり、
    main.pyのrun()自体が中断してしまっていた。process_new_emails()内のtry/exceptは
    「1件のメール処理」だけを守るもので、この生成器内部の失敗は防げなかったため、
    その回の実行ではcleanup_duplicate_transactions()等の後続処理（重複の自動整理を
    含む）に一切辿り着けず、重複が整理されないまま溜まっていく原因になっていた。
    """
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            return func(*args, **kwargs)
        except _RETRYABLE_EXCEPTIONS as e:
            # HttpErrorのうち、クォータ制限や一時的なエラー(429, 403, 5xx)以外
            # （認証エラーの401や、リクエスト自体が不正な400等）はリトライしても
            # 無駄なので、すぐに諦めて呼び出し元に投げる
            if isinstance(e, googleapiclient.errors.HttpError):
                status = getattr(e, "status_code", None) or getattr(e.resp, "status", None)
                if status not in (403, 408, 429, 500, 502, 503, 504):
                    raise
            last_error = e
            if attempt == max_attempts:
                break
            wait_seconds = 2 ** attempt  # 2, 4, 8, 16, 32秒...
            print(
                f"⚠️ Gmail APIへの通信に失敗しました（{attempt}/{max_attempts}回目）。"
                f"{wait_seconds}秒待ってリトライします: {e}"
            )
            time.sleep(wait_seconds)
    raise last_error


def _is_github_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


def get_credentials() -> Credentials:
    """認証済みのGoogle認証情報(Credentials)を返す。初回はブラウザでの認可が必要。
    Gmail・スプレッドシート両方で使い回す。

    サービス版（runner.py から呼ばれる場合）は、中継役から受け取った
    「1時間だけ使える一時的な鍵」が環境変数 GOOGLE_ACCESS_TOKEN に入っているので、それを使う。

    GitHub Actions上ではブラウザを開けないため、トークンが無い・無効な場合は
    分かりやすいエラーメッセージを出して止まる（そのまま待ち続けて時間切れに
    なるのを防ぐ）。"""
    access_token = os.environ.get("GOOGLE_ACCESS_TOKEN")
    if access_token:
        return Credentials(token=access_token)

    creds = None
    try:
        creds = Credentials.from_authorized_user_file(config.GMAIL_TOKEN_JSON, SCOPES)
    except FileNotFoundError:
        pass

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            if _is_github_actions():
                raise RuntimeError(
                    "Googleの認証トークンが無効になっています（パスワード変更・アクセス取り消しなど）。"
                    "自分のパソコンで gmail_token.json を削除してから python -m src.initial_setup を"
                    "実行し直し、新しくできた gmail_token.json の中身をGitHub SecretsのGMAIL_TOKEN_JSONに登録し直してください。"
                ) from e
            print(f"認証トークンの更新に失敗したため、もう一度ログインし直します: {e}")
            creds = None
    else:
        creds = None

    if creds is None:
        if _is_github_actions():
            raise RuntimeError(
                "Googleの認証トークン（gmail_token.json）が見つかりません。"
                "GitHub SecretsにGMAIL_TOKEN_JSONが正しく登録されているか確認してください。"
            )
        if not os.path.exists(config.GMAIL_CLIENT_SECRET_JSON):
            raise FileNotFoundError(
                f"{config.GMAIL_CLIENT_SECRET_JSON} が見つかりません。配布者から受け取った"
                f"{config.GMAIL_CLIENT_SECRET_JSON} を、このリポジトリのフォルダ（src フォルダと同じ場所）に"
                "置いてから、もう一度実行してください。"
            )
        flow = InstalledAppFlow.from_client_secrets_file(config.GMAIL_CLIENT_SECRET_JSON, SCOPES)
        creds = flow.run_local_server(port=0)

    with open(config.GMAIL_TOKEN_JSON, "w") as f:
        f.write(creds.to_json())
    return creds


def get_gmail_service():
    """認証済みのGmail APIクライアントを返す。"""
    return build("gmail", "v1", credentials=get_credentials())


def _get_or_create_label_id(service, label_name: str | None = None) -> str:
    # 既定値を引数に直接書くと、読み込み時の値に固定されてしまう（サービス版での切り替えが効かない）ため、呼ばれた時点で読む
    label_name = label_name or PROCESSED_LABEL_NAME
    labels = _call_with_retry(service.users().labels().list(userId="me").execute).get("labels", [])
    for label in labels:
        if label["name"] == label_name:
            return label["id"]
    created = _call_with_retry(
        service.users().labels().create(
            userId="me",
            body={"name": label_name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
        ).execute
    )
    return created["id"]


def _build_query(sender_configs: list[dict] | None = None) -> str:
    """送信元リストからGmail検索クエリを組み立てる（GASのbuildSenderQueryに相当）。
    sender_configsを渡さない場合はconfig.SENDER_LIST（ハードコードのデフォルト）を使う。
    「送信元リスト」シートを見て組み立てたい場合は、呼び出し側で
    sheets_client.get_sender_configs() の結果を渡すこと（このモジュール自身は
    sheets_client.pyに依存させたくないため、ここでは読み込まない）。"""
    configs = sender_configs if sender_configs is not None else config.SENDER_LIST
    parts = []
    for sender in configs:
        part = f'from:({sender["address"]})'
        excludes = sender.get("exclude_subject_keywords") or []
        if excludes:
            exclude_str = " ".join(f'-subject:({kw})' for kw in excludes)
            part = f"({part} {exclude_str})"
        parts.append(part)
    sender_query = " OR ".join(parts)
    return f"({sender_query}) -label:{PROCESSED_LABEL_NAME}{_process_after_filter()}"


def _get_plain_body(payload: dict) -> str:
    """メッセージのペイロードからプレーンテキスト本文を取り出す（再帰的にパートを探索）。
    Gmail APIはformat=full取得時に本文を自動的にUTF-8へ変換して返すため、UTF-8で
    デコードする（元のメールの実際の文字コードがISO-2022-JP等であっても、Gmail側で
    すでに変換済みのため、こちらで別の文字コードとして扱う必要はない）。"""
    if payload.get("mimeType") == "text/plain" and "data" in payload.get("body", {}):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")

    for part in payload.get("parts", []):
        result = _get_plain_body(part)
        if result:
            return result
    return ""


def fetch_unprocessed_messages(service, sender_configs: list[dict] | None = None) -> Iterator[dict]:
    """
    未処理（processedラベルが付いていない）のカード利用通知メールを、
    { 'id', 'subject', 'from', 'date', 'body' } の形でyieldする。
    GASのsearchAllThreads + 各メッセージのforEachに相当。
    sender_configsを渡した場合はそれを、渡さなければconfig.SENDER_LISTを使う
    （呼び出し側でsheets_client.get_sender_configs()の結果を渡すことを想定）。

    【重要】ラベルは「後続の処理が成功するかどうか」に関係なく、
    メールを取得した直後に付与する。後で付ける方式だと、後続の記録・LINE送信
    処理が何らかの理由で失敗した際にラベルが付かず、次回また同じメールを
    処理してしまい、取引が何重にも記録される・アラートが繰り返し届く、
    という重大な問題が起きるため。
    """
    query = _build_query(sender_configs)
    label_id = _get_or_create_label_id(service)

    page_token = None
    while True:
        resp = _call_with_retry(
            service.users().messages().list(userId="me", q=query, pageToken=page_token).execute
        )
        for msg_meta in resp.get("messages", []):
            msg = _call_with_retry(
                service.users().messages().get(userId="me", id=msg_meta["id"], format="full").execute
            )
            headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
            body = _get_plain_body(msg["payload"])

            # 先にラベルを付けて「処理済み」にする（後続処理が失敗しても再処理されないように）
            _call_with_retry(
                service.users().messages().modify(
                    userId="me", id=msg["id"], body={"addLabelIds": [label_id]}
                ).execute
            )

            yield {
                "id": msg["id"],
                "subject": headers.get("Subject", ""),
                "from": headers.get("From", ""),
                "date_ms": int(msg["internalDate"]),  # UNIXミリ秒
                "body": body,
            }

        page_token = resp.get("nextPageToken")
        if not page_token:
            break


def remove_processed_label(service, message_ids: list[str]) -> None:
    """「処理済み」の目印を外す。記録の書き込みに失敗したメールを、次回の実行でやり直せるようにするため。"""
    if not message_ids:
        return
    label_id = _get_or_create_label_id(service)
    for start in range(0, len(message_ids), 1000):  # batchModify は1回1000件まで
        _call_with_retry(
            service.users().messages().batchModify(
                userId="me",
                body={"ids": message_ids[start:start + 1000], "removeLabelIds": [label_id]},
            ).execute
        )


def fetch_unprocessed_payment_notice_messages(service) -> Iterator[dict]:
    """
    未処理の「お支払い日のご案内」メール（三井住友カード(Vpass)からの支払日確定通知）を
    { 'id', 'subject', 'body' } の形でyieldする。通常のカード利用通知メールとは
    別のラベル（PAYMENT_NOTICE_PROCESSED_LABEL）で処理済み管理する。

    【重要】こちらも fetch_unprocessed_messages と同じ理由で、
    ラベルはメール取得直後（yieldする前）に付与する。
    """
    query = (
        f'from:({config.PAYMENT_NOTICE_SENDER}) '
        f'subject:({config.PAYMENT_NOTICE_SUBJECT_KEYWORD}) '
        f'-label:{config.PAYMENT_NOTICE_PROCESSED_LABEL}'
        f'{_process_after_filter()}'
    )
    label_id = _get_or_create_label_id(service, config.PAYMENT_NOTICE_PROCESSED_LABEL)

    resp = _call_with_retry(service.users().messages().list(userId="me", q=query).execute)
    for msg_meta in resp.get("messages", []):
        msg = _call_with_retry(
            service.users().messages().get(userId="me", id=msg_meta["id"], format="full").execute
        )
        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
        body = _get_plain_body(msg["payload"])

        # 先にラベルを付けて「処理済み」にする
        _call_with_retry(
            service.users().messages().modify(
                userId="me", id=msg["id"], body={"addLabelIds": [label_id]}
            ).execute
        )

        yield {
            "id": msg["id"],
            "subject": headers.get("Subject", ""),
            "body": body,
        }


def send_email(subject: str, body: str) -> None:
    """自分のGmailアドレス宛てにメールを送信する（エラー通知用）。"""
    service = get_gmail_service()
    profile = service.users().getProfile(userId="me").execute()
    my_address = profile["emailAddress"]

    message = MIMEText(body)
    message["to"] = my_address
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    service.users().messages().send(userId="me", body={"raw": raw}).execute()