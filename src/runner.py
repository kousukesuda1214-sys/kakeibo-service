"""
家計簿サービスの親玉。GitHub Actionsで30分おきに実行される。

1. 中継役（GAS）から、連携済みの利用者の一覧と、それぞれの「1時間だけ使える一時的な鍵」を受け取る
   （長く使える鍵は中継役の外に出ない）
2. 利用者ごとに、src.user_run を別々のプロセスとして実行する（設定やキャッシュが混ざらないように）
3. 新しい利用者の家計簿の準備が済んだら、中継役に「準備済み」と伝える

■ 実行ログについて
このリポジトリは公開（public）なので、GitHub Actionsの実行ログは誰でも見られる。
そのため、利用者ごとの処理の出力（金額・店舗名などを含む）は表示せずに捨て、
「利用者#1：成功」のような結果と、エラーの場所・種類だけを表示する。
メールアドレスやLINEのIDも表示しない。

■ 環境変数（GitHub Secrets）
  RELAY_URL                  … 中継役（GAS）のウェブアプリのURL
  RUNNER_KEY                 … 中継役との合言葉
  LINE_CHANNEL_ACCESS_TOKEN  … 日次決算Botのトークン（利用者への通知に使う）
  ANTHROPIC_API_KEY          … AIによるカテゴリ判定・振り返りコメント用（任意）
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

USER_TIMEOUT_SEC = 15 * 60   # 1人あたりの処理時間の上限
FIRST_IMPORT_DAYS = 31       # 連携した日から何日前までのメールを取り込むか
RUNNER_ONLY_ENV = ("RELAY_URL", "RUNNER_KEY")  # 利用者ごとの処理には渡さない値


def relay(action: str, **params) -> dict:
    """中継役（GAS）に合言葉付きで依頼する。"""
    resp = requests.post(
        os.environ["RELAY_URL"],
        data=json.dumps({"action": action, "key": os.environ["RUNNER_KEY"], **params}),
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"中継役がエラーを返しました（{action}）: {data.get('error')}")
    return data


def _process_after(connected_at: str | None) -> str:
    try:
        connected = datetime.fromisoformat(str(connected_at).replace("Z", "+00:00"))
    except ValueError:
        connected = datetime.now(timezone.utc)
    return (connected - timedelta(days=FIRST_IMPORT_DAYS)).strftime("%Y/%m/%d")


def run_user(label: str, user: dict) -> bool:
    env = {k: v for k, v in os.environ.items() if k not in RUNNER_ONLY_ENV}
    env.update({
        "GOOGLE_ACCESS_TOKEN": user["access_token"],
        "GOOGLE_SHEET_ID": user["sheet_id"],
        "LINE_USER_ID": user["user_id"],
        "SHEET_READY": "1" if user.get("sheet_ready") else "0",
        "PROCESS_AFTER": _process_after(user.get("connected_at")),
        "KAKEIBO_SERVICE_MODE": "1",
        "TZ": "Asia/Tokyo",  # 初期値。本人の「設定」シートにタイムゾーンがあれば、実行中にそちらへ切り替わる
    })

    started = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "src.user_run"],
            env=env, capture_output=True, text=True, timeout=USER_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        print(f"{label}：⏱ 時間切れ（{USER_TIMEOUT_SEC // 60}分）")
        return False

    lines = proc.stdout.splitlines()
    if "KAKEIBO_SHEET_READY" in lines:
        try:
            relay("mark_sheet_ready", user_id=user["user_id"])
            print(f"{label}：🆕 家計簿の準備が完了しました")
        except Exception as e:
            print(f"{label}：⚠️ 準備完了を中継役に伝えられませんでした（{type(e).__name__}）")

    errors = [l.split("|") for l in lines if l.startswith("KAKEIBO_ERROR|")]
    traces = [l.split("|", 1)[1] for l in lines if l.startswith("KAKEIBO_TRACE|")]
    elapsed = time.time() - started

    if proc.returncode == 0 and not errors:
        print(f"{label}：✅ 成功（{elapsed:.0f}秒）")
        return True

    status = "❌ 失敗" if proc.returncode != 0 else "⚠️ 一部エラー"
    print(f"{label}：{status}（{elapsed:.0f}秒）")
    for _, where, kind in errors:
        print(f"    エラー：{where}（{kind}）")
    for t in traces:
        print(f"      {t}")
    if proc.returncode != 0 and not errors:
        # 想定外の止まり方（Pythonの読み込みエラーなど）。本文は出さず、例外の種類だけ表示する
        last = (proc.stderr.strip().splitlines() or ["不明"])[-1]
        print(f"    エラー：{last.split(':')[0]}")
    return False


def main() -> None:
    users = relay("list_users")["users"]
    print(f"連携済みの利用者：{len(users)}人")

    failures = 0
    for index, user in enumerate(users, start=1):
        if not run_user(f"利用者#{index}", user):
            failures += 1

    print(f"完了：成功 {len(users) - failures}人 ／ 失敗 {failures}人")
    if failures:
        # 失敗があれば、GitHub Actionsの実行を「失敗」にする。GitHubから運営者にメールが届くので、
        # LINEの通数を使わずに異常に気づける
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        # 例外の本文には、設定値（URLなど）が含まれることがあるため、種類だけを表示する
        print(f"❌ 親玉の処理が止まりました：{type(e).__name__}")
        sys.exit(1)
