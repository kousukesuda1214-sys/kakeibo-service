# 秘密の値の作り直し手順（家族以外に広げる前に1回やる）

以前、チャットのスクリーンショットに LINE のトークンと Google のクライアントシークレットが写ったため、作り直す。
**値そのものは、チャットにもスクショにも出さない**こと。コマンドは値を画面に表示しない形にしてある。

所要時間：20分ほど。晃介さん専用版（kakeibo-python）も同じLINEのトークンを使っているので、**両方を同時に**差し替える。

---

## A. Google のクライアントシークレット（先にやる。止まる時間なし）

利用者の鍵（refresh token）はクライアントIDに結びついているので、シークレットを変えても**利用者は連携し直さなくてよい**。

1. Google Cloud コンソール → プロジェクト `kakeibo-shared` →「APIとサービス」→「認証情報」→ OAuth クライアント `kakeibo-web` を開く
2. 「シークレットを追加」を押す（古いシークレットは、まだ消さない）
3. 新しいシークレットの右の📋でコピー
4. 中継役（script.google.com「家計簿サービス 中継役」）→ 左の⚙️「プロジェクトの設定」→ スクリプト プロパティ → `GOOGLE_CLIENT_SECRET` の値を貼り替えて保存
   （スクリプト プロパティは、デプロイし直さなくてもすぐ使われる）
5. 中継役のエディタで `checkSetup` を実行し、✅ が並ぶことを確認
6. LINE で「今日の決算」を押す／30分待って Actions が成功することを確認（成功＝新しいシークレットで鍵を発行できている）
7. Google Cloud に戻り、**古いシークレットを「無効にする」→「削除」**
8. 手元の控え `~/python/kakeibo-auth-test/web_client_secret.json` も、新しい JSON をダウンロードして置き換える（または使わないなら削除）

## B. LINE のチャネルアクセストークン（長期）

再発行すると古いトークンはすぐ使えなくなる。**下の 2〜5 を続けて**行う（間があくと、その間の通知が送れない）。

1. 先に `~/python/kakeibo/.env` をエディタで開いておく
2. LINE Developers → 日次決算Bot（チャネルID 2010855284）→「Messaging API設定」→ チャネルアクセストークン（長期）→「再発行」→ 📋でコピー
3. `.env` の `LINE_CHANNEL_ACCESS_TOKEN=` の後ろを貼り替えて保存
4. ターミナルで、`.env` から GitHub に登録する（値は画面に出ない）：
   ```bash
   T=$(grep '^LINE_CHANNEL_ACCESS_TOKEN=' ~/python/kakeibo/.env | cut -d= -f2- | tr -d "\"' "); printf %s "$T" | gh secret set LINE_CHANNEL_ACCESS_TOKEN -R kousukesuda1214-sys/kakeibo-service; printf %s "$T" | gh secret set LINE_CHANNEL_ACCESS_TOKEN -R kousukesuda1214-sys/kakeibo-python; unset T; echo "✅ 登録しました"
   ```
   （kakeibo-python の Secrets の名前が違う場合は `gh secret list -R kousukesuda1214-sys/kakeibo-python` で確認して合わせる）
5. 中継役のスクリプト プロパティ `LINE_CHANNEL_ACCESS_TOKEN` も貼り替えて保存
6. 確認：LINE で「今日の決算」を押してカードが返ってくる（＝中継役OK）。`gh workflow run run.yml` の実行が成功する（＝kakeibo-service OK）。専用版も次の実行で通知が届く

## C. 終わったら

- クリップボードに残った値を消す：`printf '' | pbcopy`
- 引き継ぎ書（docs/HANDOFF.md）の「残っている作業」から、この項目にチェックを付ける

## 作り直さなくてよいもの（写っていないため）

- `RUNNER_KEY`（Python と中継役の合言葉）
- `GITHUB_TOKEN`（中継役が親玉を呼ぶための鍵）
- `ANTHROPIC_API_KEY`

写ってしまったと思ったら、同じ考え方で作り直す（RUNNER_KEY は GitHub Secrets と中継役の両方を同時に）。
