"""
設定値まとめ。

■ 雛形について
このファイルの値のうち、あなた個人に関わるもの（LINEの情報、しきい値、締め日など）は
すべて環境変数（.envファイル、またはGitHub Secrets）から読み込むようにしてあります。
このファイル自体（config.py）は基本的に編集しなくても使えます。

自分好みにカスタマイズしたい場合の主な入口は2つです：
  ① .env（ローカル）/ GitHub Secrets（自動実行）… 数値や名前などの単純な設定
  ② 「送信元リスト」シート … 使っているカード会社・通販サイトの追加

カテゴリ判定のキーワード（CATEGORY_RULES）だけは、今のところこのファイルを直接
編集する形になっています（各自の生活スタイルによってキーワードの数が大きく変わり、
シート化するとかえって分かりにくくなるため）。自分の店舗名を追加したい場合は、
このファイルの CATEGORY_RULES に書き足してください。
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# 環境変数（.envファイルまたはGitHub Secretsから読み込む）
# ============================================================

# --- 必須：LINE・スプレッドシート・Gmail認証 -----------------
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_USER_ID = os.environ.get("LINE_USER_ID", "")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")  # スプレッドシートのURLに含まれるID部分

# Gmail認証（OAuth）関連のファイル名。
# GMAIL_CLIENT_SECRET_JSON は「雛形を配布している人が発行した、みんなで共有する
# OAuthクライアント」の情報が入ったファイルで、リポジトリに同梱されているものを
# そのまま使う（自分でGoogle Cloudのプロジェクトを作る必要はない）。
# GMAIL_TOKEN_JSON は、初回に自分のGoogleアカウントで認証した結果が保存される
# ファイルで、これだけは他人と共有しない・自分のGitHub Secretsに入れる。
GMAIL_TOKEN_JSON = os.environ.get("GMAIL_TOKEN_JSON", "gmail_token.json")
GMAIL_CLIENT_SECRET_JSON = os.environ.get("GMAIL_CLIENT_SECRET_JSON", "gmail_client_secret.json")

# --- 任意：自分好みに調整したい設定（未設定なら下記の初期値が使われる）---
# 締め日通知などに表示する、あなたの名前。
SENDER_NAME_LABEL = os.environ.get("SENDER_NAME_LABEL", "家計簿")

# この金額以上の利用があったら、即座にLINEへアラートを送る
HIGH_AMOUNT_THRESHOLD = int(os.environ.get("HIGH_AMOUNT_THRESHOLD", "20000"))

# カードの締め日・引落し日。三井住友カードの一般的な設定（15日締め・翌月10日払い）が
# 初期値になっているので、使っているカード会社に合わせて環境変数で上書きしてください。
CLOSING_DAY = int(os.environ.get("CLOSING_DAY", "15"))
PAYMENT_DAY = int(os.environ.get("PAYMENT_DAY", "10"))

# 「予算計画」シートにまだ該当月の行が無いときに使う、仮の予算額
SAMPLE_BUDGET_AMOUNT = int(os.environ.get("SAMPLE_BUDGET_AMOUNT", "100000"))

# この時間帯（現地時間、24時制）になっていて、まだ送っていなければ日次決算を送る
DAILY_REPORT_HOUR = int(os.environ.get("DAILY_REPORT_HOUR", "19"))
DAILY_REPORT_STATE_FILE = "daily_report_state.json"

# 週次決算を送る曜日。1=月, 2=火, ..., 7=日
WEEKLY_REPORT_WEEKDAY = int(os.environ.get("WEEKLY_REPORT_WEEKDAY", "7"))

# 締め日通知だけを、自分以外のもう一人（家族など）にも追加で送りたい場合、
# その人のLINE userIdを設定する（任意。未設定なら自分にだけ送られる）。
# 変数名はkakeibo-python（晃介専用版）のclosing_notice.py等との互換性のため
# FATHER_LINE_USER_ID のままにしてあるが、実際には家族に限らず誰でもよい。
FATHER_LINE_USER_ID = os.environ.get("FATHER_LINE_USER_ID", "")

# 月次の振り返りコメントをAIに生成してほしい場合、Anthropic APIキーを設定する
# （任意。未設定でもテンプレート文で代替されるので、無くても動く）
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# --- 任意：支払日通知メールの検知 ------------------------------
# 三井住友カード（Vpass）の「お支払い日のご案内」メールを検知する機能。
# 他のカード会社を使っていて、この機能が不要な場合は
# ENABLE_PAYMENT_NOTICE=false を設定すれば、丸ごと無効にできる。
ENABLE_PAYMENT_NOTICE = os.environ.get("ENABLE_PAYMENT_NOTICE", "true").lower() == "true"
PAYMENT_NOTICE_SENDER = os.environ.get("PAYMENT_NOTICE_SENDER", "mail@contact.vpass.ne.jp")
PAYMENT_NOTICE_SUBJECT_KEYWORD = os.environ.get("PAYMENT_NOTICE_SUBJECT_KEYWORD", "お支払い日のご案内")
PAYMENT_NOTICE_PROCESSED_LABEL = "支払日通知_処理済み"

# ============================================================
# 送信元リスト（初期値。「送信元リスト」シートを作れば、そちらが優先される）
# ============================================================
SENDER_LIST = [
    {
        "name": "三井住友カード(Vpass)",
        "address": "statement@vpass.ne.jp",
        "exclude_subject_keywords": ["ご利用確認のお願い"],
        "amount_keyword": None,
    },
    {
        "name": "Google Play",
        "address": "googleplay-noreply@google.com",
        "exclude_subject_keywords": [
            "定期購入は解約されます", "の試用は", "一時停止", "セキュリティ通信",
            "Play Points", "プロフィール", "プライバシー設定", "値上げ", "利用規約",
            "カスタマイズ", "認証", "フィッシング", "Face ID", "指紋認証",
        ],
        "amount_keyword": None,
    },
    {
        # 楽天カードは同じアドレスから宣伝メールも届くので、件名で「利用のお知らせ」だけに絞る
        # （「【速報版】カード利用のお知らせ」と、後日届く店名入りの「カード利用のお知らせ」の両方）
        "name": "楽天カード",
        "address": "info@mail.rakuten-card.co.jp",
        "include_subject_keywords": ["カード利用のお知らせ"],
        "exclude_subject_keywords": [],
        "amount_keyword": None,
    },
]

# ============================================================
# 金額抽出パターン（円建て）
# ============================================================
AMOUNT_PATTERNS = [
    r"ご利用金額[：:\s]*([\d,]+)\s*円",
    r"利用金額[：:\s]*([\d,]+)\s*円",
    r"お支払い?金額[：:\s]*([\d,]+)\s*円",
    r"合計[：:\s]*[¥￥]\s*([\d,]+)",
    # 「ご利用日｜ご利用金額」のように、見出しと値が表で横に並んでいるメール（楽天カードの速報版など）。
    # 見出しの少し後ろにある「◯◯円」を拾う（上の書き方で見つからなかったときの最後の手段）
    r"ご利用金額[\s\S]{0,80}?([\d,]+)\s*円",
]

# ============================================================
# 外貨判定
# ============================================================
CURRENCY_SYMBOL_MAP = {
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
    "₩": "KRW",
    "₹": "INR",
}
CURRENCY_CODE_LIST = ["USD", "EUR", "GBP", "AUD", "CAD", "CHF", "CNY", "KRW", "INR", "SGD", "HKD", "NZD", "THB"]

FALLBACK_RATES_TO_JPY = {
    "USD": 150, "EUR": 160, "GBP": 190, "AUD": 100, "CAD": 110, "CHF": 170,
    "CNY": 21, "KRW": 0.11, "INR": 1.8, "SGD": 112, "HKD": 19, "NZD": 92, "THB": 4.3,
}

# ============================================================
# カテゴリ判定ルール（上から順にマッチしたものを採用）
#
# ■ カスタマイズについて
# ここには、誰にでも当てはまりそうな一般的なキーワードだけを入れてあります。
# よく使うお店・サービスがここに無ければ、遠慮なく書き足してください
# （例：好きなカフェチェーン、契約しているサブスクの名前など）。
# ============================================================
CATEGORY_RULES = [
    ("交通費", ["JR", "SUICA", "PASMO", "ICOCA", "タクシー", "バス", "電車", "高速", "ETC",
               "ANA", "JAL", "航空", "空港", "メトロ", "鉄道", "UBER", "UBR", "LYFT"]),
    ("宿泊", ["ホテル", "旅館", "民泊", "AIRBNB", "楽天トラベル", "じゃらん", "HOTEL", "EXPEDIA"]),
    ("食費", ["セブン", "ローソン", "ファミリーマート", "ファミマ", "スーパー", "マクドナルド",
             "スターバックス", "カフェ", "レストラン", "イオン", "飲食", "弁当"]),
    ("サブスク・エンタメ", ["GOOGLE PLAY", "NETFLIX", "SPOTIFY", "AMAZON PRIME", "LINE",
                        "APPLE", "サブスク", "定期購入"]),
    ("通信費", ["DOCOMO", "AU", "SOFTBANK", "ソフトバンク", "携帯", "通信", "POVO"]),
    ("保険", ["INSURAN", "保険"]),
]
DEFAULT_CATEGORY = "その他"
CATEGORY_BUDGET_EXCLUDED = ["通信費", "保険"]  # 「カテゴリ予算」シートの対象外にするカテゴリ

# ============================================================
# 返金判定キーワード
# ============================================================
REFUND_KEYWORDS = ["返金", "返品", "キャンセル", "取消", "ご返金", "Refund", "Refunded"]

# ============================================================
# スプレッドシートのシート名・列構成（変更不要。構造そのものを表す値のため）
# ============================================================
SHEET_DETAIL = "取引明細"
SHEET_LOG = "ログ"
SHEET_DAILY_USAGE = "日次利用額"
SHEET_CATEGORY_BUDGET = "カテゴリ予算"
SHEET_ANNUAL_SUMMARY = "年間サマリー"
SHEET_BUDGET = "予算計画"
SHEET_SENDER_LIST = "送信元リスト"