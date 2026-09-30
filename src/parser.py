"""
メール本文から金額・店舗名・カテゴリを抽出するロジック。
GASのkosuke_template.gs（extractAmountFromMessage / extractMerchantName / categorize）を
Pythonに移植したもの。ロジックは可能な限りそのまま踏襲している。

■ 雛形化での変更点
カテゴリ判定のキーワードを、config.CATEGORY_RULES（コード）ではなく
「カテゴリルール」シート（sheets_client.get_category_rules）から読むようにした。
キーワードの追加・変更はスプレッドシート上で行える（コードの編集は不要）。

■ 2026/09/14 追加
カード会社の通知メールは「実際に利用した日」より数日遅れて届くことがある
（例：9/12にTargetで購入 → 通知メールが届いたのは9/14）。
今までは「メールが届いた時刻」を記録日にしていたため、実際の利用日とズレてしまっていた。
本文に「ご利用日時：YYYY/MM/DD」のような記載があれば、それを優先して使うようにする
extract_usage_date() を追加した。
"""

import re
import unicodedata
import requests
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from . import config, sheets_client

_JST = ZoneInfo("Asia/Tokyo")

# 為替レートAPIの結果を1回の実行内でキャッシュ（GASのexchangeRateCacheに相当）
_exchange_rate_cache: dict[str, float] = {}


@dataclass
class ExtractedAmount:
    amount: int
    match_index: int
    note: Optional[str] = None


@dataclass
class ForeignAmount:
    currency_code: str
    amount: float
    match_index: int


def get_exchange_rate_to_jpy(currency_code: str) -> float:
    """指定した外貨1単位あたりの円換算レートを取得する（実行内キャッシュ付き）。"""
    if currency_code == "JPY":
        return 1.0
    if currency_code in _exchange_rate_cache:
        return _exchange_rate_cache[currency_code]

    rate = config.FALLBACK_RATES_TO_JPY.get(currency_code, config.FALLBACK_RATES_TO_JPY["USD"])
    try:
        resp = requests.get(f"https://open.er-api.com/v6/latest/{currency_code}", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            fetched = data.get("rates", {}).get("JPY")
            if fetched:
                rate = fetched
    except requests.RequestException as e:
        print(f"為替レート取得に失敗したため概算レート({rate}円)を使用: {e}")

    _exchange_rate_cache[currency_code] = rate
    return rate


def find_foreign_currency_amount(body: str, amount_keyword: Optional[str]) -> Optional[ForeignAmount]:
    """本文から外貨建ての金額表記を探す（優先順位付き）。"""
    symbol_pattern = "|".join(re.escape(s) for s in config.CURRENCY_SYMBOL_MAP.keys())
    code_pattern = "|".join(config.CURRENCY_CODE_LIST)

    # ① 送信元設定のamount_keyword直後
    if amount_keyword:
        anchor = re.escape(amount_keyword)
        pattern = rf"{anchor}[:\s]*(?:({symbol_pattern})\s*)?([\d,]+\.?\d*)\s*(?:({code_pattern}))?"
        m = re.search(pattern, body, re.IGNORECASE)
        if m and (m.group(1) or m.group(3)):
            code = m.group(3).upper() if m.group(3) else config.CURRENCY_SYMBOL_MAP[m.group(1)]
            amount = float(m.group(2).replace(",", ""))
            return ForeignAmount(code, amount, m.start())

    # ② Order Total / Grand Total / Total
    pattern = rf"\b(?:Order Total|Grand Total|Total)\b[:\s]*(?:({symbol_pattern})\s*)?([\d,]+\.\d{{2}})\s*(?:({code_pattern}))?"
    m = re.search(pattern, body, re.IGNORECASE)
    if m and (m.group(1) or m.group(3)):
        code = m.group(3).upper() if m.group(3) else config.CURRENCY_SYMBOL_MAP[m.group(1)]
        amount = float(m.group(2).replace(",", ""))
        return ForeignAmount(code, amount, m.start())

    # ③ 金額+通貨コード（例: 12.34 USD）
    pattern = rf"([\d,]+\.\d{{2}})\s*({code_pattern})\b"
    m = re.search(pattern, body)
    if m:
        amount = float(m.group(1).replace(",", ""))
        return ForeignAmount(m.group(2).upper(), amount, m.start())

    # ④ 通貨記号のみ（例: $12.34）
    for symbol, code in config.CURRENCY_SYMBOL_MAP.items():
        pattern = re.escape(symbol) + r"\s*([\d,]+\.\d{2})"
        m = re.search(pattern, body)
        if m:
            amount = float(m.group(1).replace(",", ""))
            return ForeignAmount(code, amount, m.start())

    return None


def extract_amount_from_message(body: str, amount_keyword: Optional[str]) -> Optional[ExtractedAmount]:
    """本文から金額を抽出する（円建て優先、無ければ外貨建てを円換算）。"""
    patterns = list(config.AMOUNT_PATTERNS)
    if amount_keyword:
        anchor_pattern = re.escape(amount_keyword) + r"[：:\s]*([\d,]+)\s*円"
        patterns = [anchor_pattern] + patterns

    for pattern in patterns:
        m = re.search(pattern, body)
        if m:
            amount = int(m.group(1).replace(",", ""))
            return ExtractedAmount(amount=amount, match_index=m.start())

    foreign = find_foreign_currency_amount(body, amount_keyword)
    if foreign:
        rate = get_exchange_rate_to_jpy(foreign.currency_code)
        jpy_amount = round(foreign.amount * rate)
        note = f"{foreign.amount}{foreign.currency_code}（1{foreign.currency_code}={round(rate)}円換算）"
        return ExtractedAmount(amount=jpy_amount, match_index=foreign.match_index, note=note)

    return None


# 「ご利用日時：2026/09/12」「利用日：2026/09/15 08:47」など、
# 「ご」の有無・「時」の有無の両方の表記に対応する
_USAGE_DATE_PATTERN = re.compile(
    r"(?:ご)?利用日時?[：:]\s*(\d{4})[/／](\d{1,2})[/／](\d{1,2})(?:[^\d]{1,5}(\d{1,2}):(\d{2}))?"
)


def extract_usage_date(body: str) -> Optional[datetime]:
    """
    本文に書かれている「ご利用日時」（実際にカードを使った日）を取り出す。
    カード会社の通知メールは実際の利用日から数日遅れて届くことがあり、
    メール受信時刻をそのまま記録日にすると実際の家計と日付がズレてしまうため、
    本文にこの記載があれば、記録日としてそちらを優先的に使う。
    見つからなければNoneを返す（呼び出し側でメール受信時刻にフォールバックする）。

    ■ 2026/09/14 修正（タイムゾーンのバグ）
    「ご利用日時」は日本のカード会社が発行するため日本時間(JST)で書かれているが、
    このシステム自体は、GitHub Actionsのワークフローで指定したタイムゾーン（TZ。
    日本在住なら Asia/Tokyo）、ローカル実行時はPC本体のタイムゾーンで動いている。
    海外在住の人の場合、時刻まで書かれている即時通知をJSTのままローカルの日付として
    扱うと、実際より最大1日ズレた日付になってしまう（例：アメリカ西海岸の夕方の利用が、
    JSTでは翌日早朝の時刻になり、そのまま使うと「翌日の記録」になってしまう）。
    時刻が書かれている場合は、JSTとして解釈してからシステムのローカル
    タイムゾーンに変換する。日付だけで時刻が書かれていない場合
    （締め後にまとめて届く確定通知など）は、そのままの日付を使う
    （もともと「その日に使った」という情報であり、変換の必要がないため）。
    """
    m = _USAGE_DATE_PATTERN.search(body)
    if not m:
        # 「ご利用日｜ご利用金額」のような表の形（見出しの少し後ろに日付がある）。時刻は書かれていない
        t = _USAGE_DATE_TABLE_PATTERN.search(body)
        if not t:
            return None
        try:
            return datetime(int(t.group(1)), int(t.group(2)), int(t.group(3)))
        except ValueError:
            return None
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    has_time = m.group(4) is not None

    if has_time:
        hour, minute = int(m.group(4)), int(m.group(5))
        try:
            jst_dt = datetime(year, month, day, hour, minute, tzinfo=_JST)
        except ValueError:
            return None
        local_dt = jst_dt.astimezone()  # システムのローカルタイムゾーンに変換
        return local_dt.replace(tzinfo=None)

    try:
        return datetime(year, month, day)
    except ValueError:
        return None


_LABEL_LINE_PATTERN = re.compile(r"利用日|利用取引|お支払い|利用金額|合計|定期購入の自動更新")

# 「■利用先：ABCストア」「ご利用店名: ABCストア」のように、店名に見出しが付いている行
# （三井住友カードの「◇利用先：」は、これまでの記録と店名の形を揃えるため対象外にしている）
_MERCHANT_LABEL_PATTERN = re.compile(r"^[■●・\s]*(?:ご)?利用(?:先|店|店名|店舗|店舗名)\s*[：:]\s*(\S.*)$", re.MULTILINE)
_USAGE_DATE_TABLE_PATTERN = re.compile(r"ご?利用日[\s\S]{0,60}?(\d{4})[/／](\d{1,2})[/／](\d{1,2})")


def _is_category_only_line(line: str) -> bool:
    if re.match(r"^（[^（）]{1,12}）$", line):
        return True
    if re.match(r"^\([^()]{1,40}\)\s*[/／]?\s*[月円]{0,2}$", line):
        return True
    return False


_DECORATIVE_BULLET_PATTERN = re.compile(r"^[◇◆○●・\-*＊]+$")


def _is_decorative_bullet_line(line: str) -> bool:
    """「◇」「・」「-」のように、装飾用の記号だけで構成された行かどうかを判定する。
    HTMLメールがプレーンテキストに変換される際、区切り線などが「◇」を並べただけの
    行になることがあり（例：三井住友カードの「ご利用内容」欄）、これを店舗名として
    誤って拾わないようにする（GASのisDecorativeBulletLineに相当。Python移植時に
    抜けていたため追加した）。"""
    return bool(_DECORATIVE_BULLET_PATTERN.match(line))


_AMOUNT_ONLY_LINE_PATTERN = re.compile(r"^[¥￥]?[\d,]+\.?\d*\s*/?\s*[月円]{0,2}$")


def _is_amount_only_line(line: str) -> bool:
    """行全体が「¥17,400」「2,900」「¥360/月」のように、金額（＋通貨記号・末尾の
    「/月」など）だけで構成されているかどうかを判定する。

    ■ 2026/09/17 追加
    Google Playの「単発購入」完了メール（定期購入の更新メールとは書式が異なり、
    商品名の行と価格の行がプレーンテキスト変換時に別々の行に分かれてしまう）で、
    「合計: ¥17,400」から1行さかのぼった直近の行が、商品名ではなく価格だけの
    行（「¥17,400」）になってしまうケースがあった。この価格だけの行を店舗名として
    誤って採用してしまうバグを防ぐため、ラベル行・カッコだけの行と同様に
    読み飛ばし対象に追加した。"""
    return bool(_AMOUNT_ONLY_LINE_PATTERN.match(line))


def _strip_trailing_category_tag(line: str) -> str:
    m = re.match(r"^(.*?\S)\s*（[^（）]{1,12}）$", line)
    return m.group(1) if m else line


def _strip_trailing_amount_fragment(line: str) -> str:
    m = re.match(r"^(.*?\S)\s*[¥￥][\d,]+/?\s*$", line)
    return m.group(1) if m else line


def _clean_merchant_line(line: str) -> str:
    return _strip_trailing_amount_fragment(_strip_trailing_category_tag(line))


def extract_merchant_name(body: str, match_index: int, subject: str) -> str:
    """金額の位置から逆算して店舗名・商品名を推定する（GASのextractMerchantNameを移植）。"""
    labeled = _MERCHANT_LABEL_PATTERN.search(body)
    if labeled:
        return _clean_merchant_line(labeled.group(1))

    if match_index < 0:
        return subject

    before = body[:match_index]
    lines = [l.strip() for l in before.split("\n") if l.strip()]
    if not lines:
        return subject

    date_line_index = next((i for i, l in enumerate(lines) if "利用日" in l), -1)
    if date_line_index != -1:
        for line in lines[date_line_index + 1:]:
            if _is_category_only_line(line):
                continue
            if _is_decorative_bullet_line(line):
                continue
            if _is_amount_only_line(line):
                continue
            if _LABEL_LINE_PATTERN.search(line):
                continue
            return _clean_merchant_line(line)

    for line in reversed(lines):
        if _LABEL_LINE_PATTERN.search(line):
            continue
        if _is_category_only_line(line):
            continue
        if _is_decorative_bullet_line(line):
            continue
        if _is_amount_only_line(line):
            continue
        return _clean_merchant_line(line)

    return subject


def categorize(merchant_name: str) -> str:
    """
    店舗名からカテゴリを判定する（大文字小文字を区別しない）。

    ■ 2026/09/15 修正
    三井住友カードの確定通知メール（ISO-2022-JPベース）は、店舗名の英字が
    「ＴＡＲＧＥＴ．ＣＯＭ」のように全角で書かれてくることがある。今までは
    単純にupper()するだけだったため、CATEGORY_RULESの半角キーワード（"TARGET"等）が
    一致せず、本来「食費」等に分類されるべき店舗がずっと「その他」になっていた。
    unicodedata.normalize("NFKC", ...)で全角英数字・記号を半角に正規化してから
    判定するように修正した。

    キーワードは「カテゴリルール」シートから読む（上の行ほど優先）。
    """
    normalized = unicodedata.normalize("NFKC", merchant_name)
    upper_name = normalized.upper()
    for category, keywords in sheets_client.get_category_rules():
        # シートに全角で書かれたキーワード（「ＳＵＰＥＲ」など）も一致するよう、キーワード側も正規化する
        if any(unicodedata.normalize("NFKC", kw).upper() in upper_name for kw in keywords):
            return category
    return config.DEFAULT_CATEGORY


_ai_category_cache: dict[str, str] = {}


def ai_categorize(merchant_name: str) -> str:
    """
    キーワードルール（categorize）で判定できなかった店舗名を、Claude（Haiku）に
    推測してもらう。今まで手動で「店舗名をClaudeに見せてキーワードを追加してもらう」
    という作業が必要だったのを自動化するためのフォールバック。

    - ANTHROPIC_API_KEYが未設定、またはAPI呼び出しに失敗した場合は、
      従来通り「その他」を返す（安全側に倒す）。
    - 同じ店舗名は、この実行内ではキャッシュして重複呼び出しを避ける
      （プロセスが終わるとキャッシュは消えるため、次回の実行では再度AIに聞く。
      呼び出し回数を大きく減らしたい場合は、判明したキーワードを
      「カテゴリルール」シートに追記すれば、以後はルールで直接判定される）。
    """
    if not config.ANTHROPIC_API_KEY:
        return config.DEFAULT_CATEGORY

    key = merchant_name.strip()
    if not key:
        return config.DEFAULT_CATEGORY
    if key in _ai_category_cache:
        return _ai_category_cache[key]

    options = [name for name, _ in sheets_client.get_category_rules()]
    if config.DEFAULT_CATEGORY not in options:
        options.append(config.DEFAULT_CATEGORY)
    prompt = (
        "クレジットカードの利用明細に表示された、次の店舗名・商品名から、"
        "最も当てはまるカテゴリを下の選択肢から1つだけ選んでください。\n"
        f"選択肢: {', '.join(options)}\n"
        f"店舗名・商品名: {key}\n"
        "出力は選択肢の中のカテゴリ名1つだけにしてください（説明や記号、句読点は不要です）。"
    )

    category = config.DEFAULT_CATEGORY
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
                "max_tokens": 20,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        text = "".join(
            block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
        ).strip()
        if text in options:
            category = text
    except requests.RequestException as e:
        print(f"AIカテゴリ判定に失敗しました（{key}）: {e}")

    _ai_category_cache[key] = category
    return category


def is_refund(subject: str, body: str) -> bool:
    text = subject + body
    return any(kw in text for kw in config.REFUND_KEYWORDS)
