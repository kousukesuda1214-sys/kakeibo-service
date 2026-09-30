# 家計簿サービス 引き継ぎ書（HANDOFF）

最終更新：2026/09/29（Claudeとのチャットで作成）

このファイルは、**新しいチャットのClaudeが、このプロジェクトの経緯と今の状態を一度で把握するため**のもの。
コードはすべてGitHubの公開リポジトリにあるので、**晃介さんにコードの貼り付けを求めず**、自分で読みに行くこと
（bash の curl で `raw.githubusercontent.com` / `api.github.com` が使える。web_fetch はユーザーのメッセージに書かれたURLのみ）。

---

## 0. 次のチャットのClaudeへ（最初にやること）

1. このファイルを最後まで読む
2. ファイル一覧を取る：
   `curl -s "https://api.github.com/repos/kousukesuda1214-sys/kakeibo-service/git/trees/main?recursive=1" | python3 -c "import json,sys;[print(t['path']) for t in json.load(sys.stdin)['tree']]"`
3. 必要なファイルを読む：`curl -s https://raw.githubusercontent.com/kousukesuda1214-sys/kakeibo-service/main/<パス>`
   （キャッシュで古い版が返ることがある。最新のコミットを使うなら `.../kakeibo-service/<コミットID>/<パス>`）
4. 晃介さんに「どこから再開するか」を、「6. 残っている作業」から確認する

---

## 1. これは何か

クレジットカードの利用通知メール（Gmail）を自動で読み取り、**利用者本人のGoogleスプレッドシート**に記録し、
**LINE公式アカウント「日次決算Bot」**で日次・週次の決算や高額アラートを届ける家計簿サービス。

- 運営者：晃介さん（GitHub：`kousukesuda1214-sys`、Google：kousukesuda1214@gmail.com、アメリカ在住・タイムゾーン America/Los_Angeles）
- 利用者：家族・友人（多くは日本在住、MacとWindowsが混在、プログラミングに詳しくない）
- 目標：**利用者はLINEとスプレッドシートだけで完結**（Pythonやパソコンでの設定は一切不要）。アプリのように運営が管理する形

### 経緯（ざっくり）
1. もともと **GAS版**（スプレッドシートに埋め込んだGoogle Apps Script）で動いていた。晃介さん以外にもGAS版を使っている人がいる
2. 晃介さん専用に **Python版 `kakeibo-python`**（非公開リポジトリ、ローカルは `~/python/kakeibo`）へ移植。今も晃介さん自身はこれを使っている
3. 配布用に **雛形 `kakeibo-template`**（公開）を作ったが、「各自がGitHubとPythonを用意する」形は利用者に難しいため**方針転換**し、この仕組みは使わなくなった
4. 現在の形：**`kakeibo-service`（このリポジトリ）** ＋ **中継役（GAS）** ＋ **連携ページ `kakeibo-auth`** で、運営者が全員分をまとめて処理する

---

## 2. 全体の仕組み

```
[利用者のLINE] ─友だち追加・ボタン─▶ [中継役（GAS）] ─連携リンク・カード型の返信（返信は無料）
      │                                   ▲  鍵（refresh token）はここにだけ保管（スクリプトプロパティ）
      ▼ リンク                             │
[連携ページ kakeibo-auth（GitHub Pages）] ─Googleの許可コード─▶ 中継役が鍵に交換・本人のドライブに家計簿を作成
                                                                 └▶ GitHub API で親玉をすぐ実行（2〜3分で準備完了）
[親玉 kakeibo-service（GitHub Actions・30分おき）]
   src/runner.py：中継役から「利用者一覧＋1時間だけ使える鍵」を受け取り、1人ずつ別プロセスで src/user_run.py を実行
   src/user_run.py：初回はシート作成 → src/main.py（Gmail読み取り→記録→決算）→ 見た目・並び順 → 最新の数字（スナップショット）
   runner.py はスナップショットを中継役に預ける（LINEのボタンで即答するため）
```

### 権限の方針（大事）
- Googleの権限は最小限：`gmail.modify` と `drive.file`（**このシステムが作ったファイルだけ**）。利用者の他のファイルには触れない
- 長く使える鍵は中継役の外に出さない。Pythonには1時間で失効する access token だけ渡す
- 公開リポジトリなので、**Actionsの実行ログに個人情報を出さない**（runner.py が利用者ごとの出力を捨て、結果とエラーの場所・種類だけ表示）
- エラーは利用者ではなく運営者へ（Actionsの失敗 → GitHubからのメール）

---

## 3. 部品と場所

| 部品 | 場所 | 反映のしかた |
|---|---|---|
| 親玉・家計簿の処理（Python） | GitHub `kakeibo-service`（公開）／ローカル `~/python/kakeibo-service` | push すると次の実行（30分以内）で全員に反映 |
| 中継役（GAS） | script.google.com「家計簿サービス 中継役」（スプレッドシートから作ったプロジェクト。そのスプレッドシートは消さない）。控え：`gas/relay/Code.gs` | エディタに貼って保存 →「デプロイ」→「デプロイを管理」→ ✏️ →「新バージョン」（**新しいデプロイにするとURLが変わるので禁止**） |
| 連携ページ | GitHub `kakeibo-auth`（公開・GitHub Pages）`https://kousukesuda1214-sys.github.io/kakeibo-auth/` ／ローカル `~/python/kakeibo-auth` | push で1〜2分で反映 |
| リッチメニュー画像 | `kakeibo-auth/richmenu.png`（作成コード：`tools/richmenu/draw.py`） | 画像をpush → GASで `setupRichMenu` を実行 |
| GAS版からの引っ越しコード | `gas/migrate/migrate.gs` | 利用者が自分のGAS版の家計簿の Apps Script に貼って `migrateToNewKakeibo` を実行 |
| 晃介さん専用版 | GitHub `kakeibo-python`（非公開）／ローカル `~/python/kakeibo` | 今回のサービスとは別。触るときは要確認 |
| 旧・雛形 | GitHub `kakeibo-template`（公開・Template repository） | 使っていない（Google OAuthのブランディングのURLがこのREADMEを指している） |
| 検証用の控え | ローカル `~/python/kakeibo-auth-test`（`web_client_secret.json`・`runner_key.txt`・`exchange_test.py`） | GitHubには上げない |

### Google Cloud（プロジェクト `kakeibo-shared`、ID `kakeibo-shared-509402`）
- 公開ステータス：**本番環境**（未審査。「このアプリは確認されていません」の警告が出る。生涯100ユーザーまで）
- ブランディング：ホームページ／プライバシーポリシーは `kakeibo-template` のURL、承認済みドメイン `github.com`・`kousukesuda1214-sys.github.io`（**これらが無いと本番公開ボタンが押せなかった**）
- OAuthクライアント：`kakeibo-web`（ウェブ。リダイレクトURI `https://kousukesuda1214-sys.github.io/kakeibo-auth/`）← 現在使用中。`kakeibo-shared-client`（デスクトップ）は旧雛形用

### LINE
- 日次決算Bot（@681ufyix、チャネルID 2010855284、無料のコミュニケーションプラン＝**月200通を全員で共有**。返信は無料・pushだけが数えられる）
- Webhook URL＝中継役のウェブアプリURL（`kakeibo-auth/index.html` の `RELAY_URL` にも書いてある）
- 応答メッセージ・あいさつメッセージ：オフ（あいさつは中継役の「ようこそ」カードが担当）。「チャット」もオフ推奨（「担当者が返信します」表示を消すため）
- リッチメニューは Messaging API で登録（OA Managerで作ったメニューより優先される。**OA Managerでは変えない**）

### 秘密の値（名前だけ。値はここに書かない）
- GitHub Secrets（kakeibo-service）：`RELAY_URL`・`RUNNER_KEY`・`LINE_CHANNEL_ACCESS_TOKEN`・`ANTHROPIC_API_KEY`
- GASスクリプトプロパティ：`LINE_CHANNEL_ACCESS_TOKEN`・`GOOGLE_CLIENT_ID`・`GOOGLE_CLIENT_SECRET`・`RUNNER_KEY`・`GITHUB_TOKEN`（fine-grained、kakeibo-serviceの Actions: Read and write のみ、期限なし）
  ＋自動保存：`user:<LINE userId>`（鍵・メール・sheet_id・sheet_ready・connected_at）・`state:<uuid>`（連携リンク・1時間）・`snap:<userId>`（最新の数字）
- LINEトークン・ANTHROPIC_API_KEY の元は `~/python/kakeibo/.env`。**LINEのトークンは再発行しない**（晃介さん専用版が止まる）

---

## 4. 主な機能（すべて実装・テスト済み）

**LINE（中継役）**
- 友だち追加 →「家計簿へようこそ！」カード（3ステップ＋「Googleと連携する」ボタン）。連携済みのブロック解除は「おかえりなさい」
- リッチメニュー6ボタン：今日の決算／家計簿を開く／設定／今月のカテゴリ別／使い方／連携・解除（すべてFlexのカードで返信）
- 「設定」はスプレッドシートの「設定」シートをその場で読む。日次決算の曜日・週次決算の曜日（月〜日のトグル＋毎日/平日/週末）、日次決算の時刻、高額アラート金額は **LINEから変更でき、シートに書き込む**（設定の正はシートだけ）
- 連携解除は確認ダイアログ付き。ブロック／解除で鍵削除＋Googleの許可取り消し（シートは残る）。偽の退会イベントはLINEのプロフィールAPIで確認
- 連携完了時に GitHub API で親玉をすぐ実行

**家計簿（Python）**
- 設定はすべて「設定」シート（`src/settings.py`）：タイムゾーン、締め日（「末日」可）、引落し日、日次決算の時刻、**日次決算を送る曜日（週1〜7回）**、**週次決算を送る曜日（送らない可）**、高額アラート金額、仮の予算、通知に表示する名前、支払日通知。項目が増えたら関係する項目の下に自動で差し込み、説明文も自動更新。読めない値は状態欄に理由を出して初期値で動く
- 日次決算を毎日送らない場合は「🧾 前回（m/d）からの利用」を表示。週次決算は前回の送信時刻の直後〜今を集計（重複なし）
- 見た目は `src/sheet_style.py` の `LAYOUT_VERSION` で管理（上げると全員に反映）。シートの並び順も毎回そろえる
- 初回は直近31日分のメールだけ取り込む（`PROCESS_AFTER`）。処理済みラベルは `家計簿サービス_処理済み`（専用版の `日次決算_処理済み` と別）
- 記録は20件ずつまとめて書き込み（APIの回数制限対策。100件で870回→25回）。429は65秒待って再試行。書き込み失敗時は処理済みラベルを外して次回やり直す
- 同じエラーのLINE通知は1日1回（`notify.py`）。サービス版ではエラーは運営者側へ
- LINEの月の上限（429 monthly limit）はエラーにせず諦める
- **楽天カード対応**：送信元リストに F列「読み取る件名」（楽天は「カード利用のお知らせ」だけ＝宣伝メールを読まない）。HTMLだけのメールも文字に変換して読む。表形式の金額・利用日を読める。**速報版**は「楽天カード（速報・店名は後日）」で即記録し、店名入りの通知が来たら同じ日・同じ金額の速報行を消してまとめる（`reconcile_flash_reports`）
- 三井住友カードの即時通知/確定通知のまとめ、重複整理、年間サマリー、サブスク棚卸し、ヘルスチェック、振り返りコメント（AI）など、専用版の機能は一通りある

**GAS版からの引っ越し**：`gas/migrate/migrate.gs`（利用者が自分で実行する方式）。取引明細・ログは新しい家計簿に無い期間だけコピー、予算計画は項目ごとにマージ、カテゴリ予算・送信元リストは置き換え、GAS版のトリガーを削除。何度実行しても重複しない

---

## 5. 決めたこと（方針）

- 利用者のリポジトリは作らない（全員分をこのリポジトリの Actions でまとめて処理）。このリポジトリは公開（Actions無制限のため）
- AI（Claude）はカテゴリ判定・振り返りコメントに全員分使う（1人月数円の見込み）。エラー診断のAIは利用者向けには使わない
- LINEは無料プランのまま様子を見る。日次決算を減らす／返信で見る方式で節約できる
- 「設定」シートのプルダウン化は見送り（LINEから設定を変えられるようにしたため）
- 既存の利用者のシートは、値を消さずに自動で新しい形にそろえる（行の差し込み・説明文・版数・並び順）。ただし「予算計画」の2段見出しのような**形そのものの作り替えは新しい家計簿だけ**
- 晃介さん専用版はサービス版に移らない（15分おき・お父様への支払日通知などがあるため）。GitHub Actionsの無料枠を9月分で使い切り、**10月1日のリセットまで待つ**ことにした（止まっていた分は再開後にまとめて記録される）

---

## 6. 残っている作業・確認待ち

- [ ] **楽天カードの店名入りの通知の形を実物で確認**（想定：`■利用先: ○○`。店名が件名になっていたら直す）
- [ ] **GAS版からの引っ越しを実際の利用者（恵子さんなど）で試す**。つまずいた所を手順・コードに反映
- [ ] 家族にQRから友だち追加してもらい、「ようこそ」→連携→2〜3分で準備、の流れを実機で確認
- [ ] 「連携解除→同じアカウントで連携し直し」で**空の家計簿が新しく作られる**問題（過去のメールは処理済みラベル付きで取り込まれない）。同じメールなら前の家計簿を使い続けたい（drive.file の権限が再連携後も前のファイルに効くかの確認が必要）
- [ ] プライバシーポリシー（README）を今の仕組み（中継役に鍵を保管・運営者が処理）に合わせて更新し、Google Cloud のブランディングのURLを `kakeibo-service` に向け直す
- [ ] 家族以外に広げる前に、LINEトークン・Googleのクライアントシークレットを作り直す（以前チャットのスクショに写ったため）
- [ ] `actions/checkout@v4`・`setup-python@v5` を Node 24 対応版に上げる（Actionsの警告）
- [ ] `notify.py` の同一エラー1日1回の間引きを、晃介さん専用版 `kakeibo-python` にも移植
- [ ] 利用者が増えたら：LINE有料プラン、状態で出し分けるリッチメニュー（連携前は2ボタン）、「お父様への支払日通知」のような追加の送り先
- `src/rebuild_all_transactions.py`・`recategorize_all_transactions.py` はローカル用の古い道具で、サービス版の流れ（速報版・件名の絞り込み）には未対応

---

## 7. 晃介さんとのやりとりの約束ごと（とても大事）

- **日本語・やさしい言葉**で、手順は番号付き・1ステップずつ。専門用語には一言説明
- ファイルは**zipでまとめて渡し**、反映は次の形のコマンドで（ダウンロードフォルダの同名ファイルの取り違えを防ぐ）：
  ```bash
  cd ~/python/kakeibo-service
  unzip -o "$(ls -t ~/Downloads/kakeibo-service-updateN*.zip | head -1)"
  python -m py_compile src/*.py && echo "✅ 構文エラーなし"
  git add -A && git commit -m "…" && git push
  ```
- GASのコードは `pbcopy < "$(ls -t ~/Downloads/Code*.gs | head -1)"` でコピー → エディタに貼る → 「デプロイを管理」→「新バージョン」
- 実行と結果確認は gh（ログイン済み）で：
  ```bash
  gh workflow run run.yml && sleep 5 && RUN=$(gh run list --workflow=run.yml --limit 1 --json databaseId --jq '.[0].databaseId') && gh run watch "$RUN" --exit-status > /dev/null; gh run view "$RUN" --log | grep -E "利用者|完了|エラー" | sed 's/.*Z //'
  ```
- Secrets の登録は `gh secret set NAME`（値を画面に出さない）。**チャット欄のコピーボタンの中身（コマンドの文字）をそのままGitHubに貼ってしまった失敗がある**ので、コピー＆ペーストの行き来は避ける
- 秘密の値が写った画面はスクショを送らないよう毎回ひと声かける
- VS Codeで新規ファイルを作って貼る方式は、ファイル名・場所がずれやすかった（「Annual summary」「parser . py」など）。**ダウンロード＋コマンドで入れる**
- 変更は**Claude側で模擬環境のテストをしてから**渡す（GASはNodeでモック、Pythonは偽のスプレッドシートで範囲チェック付き）。pushやデプロイは全員に即反映されるため
- zshのプロンプトに複数行を貼ると、途中で切れて `for>` や `dquote>` で止まることがある → `control + C` で戻る。長いコマンドは1行にまとめる
