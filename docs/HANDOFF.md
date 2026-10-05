# 家計簿サービス 引き継ぎ書（HANDOFF）

最終更新：2026/10/04（2回目のチャット：9/29〜10/4。再連携の問題・Node 24対応・プライバシーポリシー・楽天の表形式・Google CloudのURL向け直し・専用版の回数制限と無料枠・**GitHubの定期実行が飛ばされる問題（GASから30分おきに起動）**）

このファイルは、**新しいチャットのClaude（claude.ai でも Claude Code でも）が、このプロジェクトの経緯と今の状態を一度で把握するため**のもの。
コードはすべてGitHubにあるので、**晃介さんにコードの貼り付けを求めず**、自分で読みに行くこと。
- **claude.ai のチャット**：bash の curl で `raw.githubusercontent.com` が使える。`api.github.com` は共有IPですぐ回数制限に当たるので、
  `git clone https://github.com/kousukesuda1214-sys/<リポジトリ>.git` で読む。非公開の `kakeibo-python` は読めない。push もできない（zipで渡す）
- **Claude Code（2026/10/04 に晃介さんのMacへ導入。Claude Desktop の Code タブ）**：`~/python/kakeibo-service`・`~/python/kakeibo`（専用版）を直接読み書きでき、
  晃介さんの `git`・`gh` のログインで commit・push・Actions の確認までできる。**push は全員に即反映されるので、必ず内容を見せて許可をもらってから**

---

## 0. 次のチャットのClaudeへ（最初にやること）

1. このファイルを最後まで読む
2. コードを読む：
   - Claude Code：そのまま `~/python/kakeibo-service` を読む。まず `git status`・`git log --oneline -5` で未push の変更が無いか確認
   - claude.ai：`git clone https://github.com/kousukesuda1214-sys/kakeibo-service.git` して読む
     （`raw.githubusercontent.com` はキャッシュで古い版が返ることがある。`api.github.com` はすぐ回数制限に当たる）
4. **前回のチャットの変更が反映済みか確認する**：
   - `git log` に「30分おきの起動は中継役から・定期実行は2時間おきの予備に」のコミットがあるか（10/04 時点で**未push の可能性あり**。
     中身は `.github/workflows/run.yml` の cron を `17 */2 * * *` に、`gas/relay/Code.gs` に `kickRunner`・`setupKickTrigger` を追加）
   - script.google.com の中継役に `kickRunner` があり、⏰トリガーに `kickRunner`（30分おき）が1つあるか
   - 両方のリポジトリの Actions に「Manually run」（workflow_dispatch）が30分おきに並んでいるか
5. 晃介さんに「どこから再開するか」を、「6. 残っている作業」から確認する

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
[親玉 kakeibo-service（GitHub Actions）] ◀─30分おきに手動実行として起動─ 中継役の時間主導トリガー（kickRunner）
   （GitHubの定期実行 schedule は混雑時に飛ばされ数時間あくため。schedule は2時間おきの予備として残す）
   src/runner.py：中継役から「利用者一覧＋1時間だけ使える鍵」を受け取り、1人ずつ別プロセスで src/user_run.py を実行
   src/user_run.py：初回はシート作成 → src/main.py（Gmail読み取り→記録→決算）→ 見た目・並び順 → 最新の数字（スナップショット）
   runner.py はスナップショットを中継役に預ける（LINEのボタンで即答するため）
```

### 連携解除 → 連携し直し（2026/09/29 対応）
- 解除・ブロック・連携切れ（invalid_grant）のとき、中継役は鍵を消すが、`past:<メールのSHA-256>` に
  「家計簿のファイルID・準備済みか・最初に連携した日」だけを約13か月（400日）残す
- 同じGoogleアカウントで連携し直すと（LINEアカウントが違ってもよい）、新しい鍵で前の家計簿を開けるかを Drive API で確認
  - 開ける → 前の家計簿を使い続ける。取り込む範囲は「最初に連携した日の31日前」から（解除していた間のメールも拾う。処理済みラベル付きは読まないので二重にならない）
  - 開けない（drive.file の権限が戻らない・ごみ箱）→ 新しい家計簿を作り、`user_run.prepare_reimport()` が**家計簿が空のときだけ**処理済みラベルを外して取り込み直す
- **drive.file の権限が再連携後も前のファイルに効くかは、まだ実機で未確認**（どちらでも動くように作ってある。連携ページの文言でどちらになったか分かる）
- 同じLINE・同じGoogleアカウントで「連携し直す」を押したときは、古い鍵を**取り消さない**（Googleの取り消しはアプリへの許可ごと外すため、新しい鍵まで無効になるおそれがあった）
- 1回の実行で読むメールは300件まで（`runner.MAX_MESSAGES_PER_RUN`）。残りは次の実行へ（時間切れで目印だけ付いて記録されない、を防ぐ）。取り込む範囲は最長400日（`MAX_IMPORT_DAYS`）

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
| 晃介さん専用版 | GitHub `kakeibo-python`（非公開）／ローカル `~/python/kakeibo`。ワークフロー `daily.yml` | 今回のサービスとは別。触るときは要確認。push で反映 |
| 専用版の起動役（GAS） | script.google.com「家計簿 専用版 起動役」（単独のプロジェクト）。控え：`gas/kick-python/kick.gs` | 30分おきに `kakeibo-python` の `daily.yml` を手動実行として起動。スクリプト プロパティ `GITHUB_TOKEN` |
| 旧・雛形 | GitHub `kakeibo-template`（公開・Template repository） | 使っていない（2026/09/30 にブランディングのURLを kakeibo-service へ向け直したので、もう参照されていない） |
| プライバシーポリシー | `docs/PRIVACY.md`（READMEから要約とリンク。Google Cloud のブランディングもここを指す） | push で反映 |
| 秘密の値の作り直し手順 | `docs/SECRETS_ROTATION.md` | 家族以外に広げる前に1回やる |
| 検証用の控え | ローカル `~/python/kakeibo-auth-test`（`web_client_secret.json`・`runner_key.txt`・`exchange_test.py`） | GitHubには上げない |

### Google Cloud（プロジェクト `kakeibo-shared`、ID `kakeibo-shared-509402`）
- 公開ステータス：**本番環境**（未審査。「このアプリは確認されていません」の警告が出る。生涯100ユーザーまで）
- ブランディング（2026/09/30 向け直し済み）：ホームページ `https://github.com/kousukesuda1214-sys/kakeibo-service`、プライバシーポリシー `https://github.com/kousukesuda1214-sys/kakeibo-service/blob/main/docs/PRIVACY.md`、利用規約は空欄。「ブランディングを確認」（審査の申し込み）は押さない。承認済みドメイン `github.com`・`kousukesuda1214-sys.github.io`（**これらが無いと本番公開ボタンが押せなかった**）
- OAuthクライアント：`kakeibo-web`（ウェブ。リダイレクトURI `https://kousukesuda1214-sys.github.io/kakeibo-auth/`）← 現在使用中。`kakeibo-shared-client`（デスクトップ）は旧雛形用

### LINE
- 日次決算Bot（@681ufyix、チャネルID 2010855284、無料のコミュニケーションプラン＝**月200通を全員で共有**。返信は無料・pushだけが数えられる。
  **専用版も同じBot・同じ枠**。9月は専用版だけで上限に達した。月の区切りは日本時間。10/04 時点の今月の使用は2通。確認：
  `T=$(grep '^LINE_CHANNEL_ACCESS_TOKEN=' ~/python/kakeibo/.env | cut -d= -f2- | tr -d "\"' "); curl -s -H "Authorization: Bearer $T" https://api.line.me/v2/bot/message/quota/consumption; unset T`）
- Webhook URL＝中継役のウェブアプリURL（`kakeibo-auth/index.html` の `RELAY_URL` にも書いてある）
- 応答メッセージ・あいさつメッセージ：オフ（あいさつは中継役の「ようこそ」カードが担当）。「チャット」もオフ推奨（「担当者が返信します」表示を消すため）
- リッチメニューは Messaging API で登録（OA Managerで作ったメニューより優先される。**OA Managerでは変えない**）

### 秘密の値（名前だけ。値はここに書かない）
- GitHub Secrets（kakeibo-service）：`RELAY_URL`・`RUNNER_KEY`・`LINE_CHANNEL_ACCESS_TOKEN`・`ANTHROPIC_API_KEY`
- GASスクリプトプロパティ（中継役）：`LINE_CHANNEL_ACCESS_TOKEN`・`GOOGLE_CLIENT_ID`・`GOOGLE_CLIENT_SECRET`・`RUNNER_KEY`・`GITHUB_TOKEN`（fine-grained、kakeibo-serviceの Actions: Read and write のみ、期限なし）
- GASスクリプトプロパティ（専用版の起動役）：`GITHUB_TOKEN`（fine-grained「kakeibo-python 起動役」、kakeibo-python の Actions: Read and write のみ、期限なし。2026/10/04 作成）
  ＋自動保存：`user:<LINE userId>`（鍵・メール・sheet_id・sheet_ready・connected_at・first_connected_at）・`state:<uuid>`（連携リンク・1時間）・`snap:<userId>`（最新の数字）・`past:<メールのSHA-256>`（解除した人の家計簿の控え・400日）
- LINEトークン・ANTHROPIC_API_KEY の元は `~/python/kakeibo/.env`。**LINEのトークンは再発行しない**（晃介さん専用版が止まる）

---

## 4. 主な機能（すべて実装・テスト済み）

**LINE（中継役）**
- 友だち追加 →「家計簿へようこそ！」カード（3ステップ＋「Googleと連携する」ボタン）。連携済みのブロック解除は「おかえりなさい」
- リッチメニュー6ボタン：今日の決算／家計簿を開く／設定／今月のカテゴリ別／使い方／連携・解除（すべてFlexのカードで返信）
- 「設定」はスプレッドシートの「設定」シートをその場で読む。日次決算の曜日・週次決算の曜日（月〜日のトグル＋毎日/平日/週末）、日次決算の時刻、高額アラート金額は **LINEから変更でき、シートに書き込む**（設定の正はシートだけ）
- 連携解除は確認ダイアログ付き。ブロック／解除で鍵削除＋Googleの許可取り消し（シートは残る）。偽の退会イベントはLINEのプロフィールAPIで確認
- 中継役の時間主導トリガー `kickRunner` が30分おきに親玉を手動実行として起動（初回のみエディタで `setupKickTrigger` を実行。失敗するとGoogleからエラーのメール）
- 連携完了時に GitHub API で親玉をすぐ実行（連携し直しで前の家計簿を使い続ける場合も、解除中のメールを拾うためすぐ実行）
- 同じGoogleアカウントで連携し直すと前の家計簿の続きから使える（上の「連携解除 → 連携し直し」）

**家計簿（Python）**
- 設定はすべて「設定」シート（`src/settings.py`）：タイムゾーン、締め日（「末日」可）、引落し日、日次決算の時刻、**日次決算を送る曜日（週1〜7回）**、**週次決算を送る曜日（送らない可）**、高額アラート金額、仮の予算、通知に表示する名前、支払日通知。項目が増えたら関係する項目の下に自動で差し込み、説明文も自動更新。読めない値は状態欄に理由を出して初期値で動く
- 日次決算を毎日送らない場合は「🧾 前回（m/d）からの利用」を表示。週次決算は前回の送信時刻の直後〜今を集計（重複なし）
- 見た目は `src/sheet_style.py` の `LAYOUT_VERSION` で管理（上げると全員に反映）。シートの並び順も毎回そろえる
- 初回は直近31日分のメールだけ取り込む（`PROCESS_AFTER`）。処理済みラベルは `家計簿サービス_処理済み`（専用版の `日次決算_処理済み` と別）
- 記録は20件ずつまとめて書き込み（APIの回数制限対策。100件で870回→25回）。429は65秒待って再試行。書き込み失敗時は処理済みラベルを外して次回やり直す
- 同じエラーのLINE通知は1日1回（`notify.py`）。サービス版ではエラーは運営者側へ
- LINEの月の上限（429 monthly limit）はエラーにせず諦める
- **楽天カード対応**：送信元リストに F列「読み取る件名」（楽天は「カード利用のお知らせ」だけ＝宣伝メールを読まない）。HTMLだけのメールも文字に変換して読む。表形式の金額・利用日を読める。**速報版**は「楽天カード（速報・店名は後日）」で即記録し、店名入りの通知が来たら同じ日・同じ金額の速報行を消してまとめる（`reconcile_flash_reports`）
- メール1通を記録1行にする処理は `main.parse_card_email()` に共通化（定期実行と `rebuild_all_transactions.py` が同じ読み方をする）
- 店名の読み取り：結果が日付だけ・見出しだけ・件名そのもの、なら**表の形として読み直す**（縦の表「ご利用先」の次の行／横の表「ご利用日・ご利用先…」の見出しと値の並び）。利用日は「2026年9月28日」の形も読める。楽天の店名入り通知がHTMLの表で来ても読めるはず（実物は未確認）
- 三井住友カードの即時通知/確定通知のまとめ、重複整理、年間サマリー、サブスク棚卸し、ヘルスチェック、振り返りコメント（AI）など、専用版の機能は一通りある

**GAS版からの引っ越し**：`gas/migrate/migrate.gs`（利用者が自分で実行する方式）。取引明細・ログは新しい家計簿に無い期間だけコピー、予算計画は項目ごとにマージ、カテゴリ予算・送信元リストは置き換え、GAS版のトリガーを削除。何度実行しても重複しない

---

## 5. 決めたこと（方針）

- 利用者のリポジトリは作らない（全員分をこのリポジトリの Actions でまとめて処理）。このリポジトリは公開（Actions無制限のため）
- AI（Claude）はカテゴリ判定・振り返りコメントに全員分使う（1人月数円の見込み）。エラー診断のAIは利用者向けには使わない
- LINEは無料プランのまま様子を見る。日次決算を減らす／返信で見る方式で節約できる
- 「設定」シートのプルダウン化は見送り（LINEから設定を変えられるようにしたため）
- 既存の利用者のシートは、値を消さずに自動で新しい形にそろえる（行の差し込み・説明文・版数・並び順）。ただし「予算計画」の2段見出しのような**形そのものの作り替えは新しい家計簿だけ**
- 連携を解除した人の「どの家計簿を使っていたか」の控えは、メールアドレスをSHA-256に変換して400日残す（プライバシーポリシーに明記。すぐ消したい人は運営者に連絡）
- 秘密の値の作り直しは、家族以外に広げる前にまとめて行う（`docs/SECRETS_ROTATION.md`）
- **GitHub の定期実行（schedule）には頼らない**：2026/10 に、30分おきの設定なのに3〜6時間おきにしか動かない状態を確認（両リポジトリとも）。
  日次決算は「送信時刻から2時間以内に実行されたときだけ送る」ので、間引かれると届かない日が出る（専用版で 10/2・10/3 に発生）。
  → 起動は GAS の時間主導トリガーから workflow_dispatch（飛ばされない）。schedule は予備（サービス版2時間おき・専用版6時間おき）
- Claude の契約はお父様の名義（Pro）のまま使う（晃介さんが判断。規約上アカウントの共有は認められていない点は伝えた）
- 晃介さん専用版はサービス版に移らない（15分おき・お父様への支払日通知などがあるため）。GitHub Actionsの無料枠を9月分で使い切り、**10月1日のリセットまで待つ**ことにした（止まっていた分は再開後にまとめて記録される）

---

## 6. 残っている作業・確認待ち

**2026/09/29（2回目のチャット）で対応したもの**（zip `kakeibo-service-update-20260929.zip` と `Code.gs` で渡した。反映済みかは「0.」の4で確認）
- [x] 連携解除 → 連携し直しで空の家計簿が作られる問題（中継役の `past:` 控え＋Pythonの取り込み直し）
- [x] 同じアカウントで「連携し直す」と新しい鍵まで取り消されるおそれがあった問題
- [x] `actions/checkout@v7`・`actions/setup-python@v7`（Node 24。入力の変更なし）
- [x] プライバシーポリシーを今の仕組みに合わせて `docs/PRIVACY.md` に（スナップショットと解除後の控えも明記）
- [x] `rebuild_all_transactions.py` を件名の絞り込み・楽天の速報版に対応（`parse_card_email` を共通で使う）。サービス版では使わない旨を明記
- [x] 楽天の店名入り通知が表の形でも読めるように（実物での確認は下に残す）
- [x] 秘密の値の作り直し手順書 `docs/SECRETS_ROTATION.md`

**晃介さんの作業・実機での確認が必要なもの**
- [ ] **連携し直しの実機テスト**：LINEの「連携・解除」→解除 → 何か送る → 同じGoogleアカウントで連携。連携ページの文言が
      「前の家計簿をそのまま使います」なら drive.file の権限は戻る／「前の家計簿を開けなかった」なら戻らない。**結果をここに書く**
- [x] Google Cloud のブランディングのURLを `kakeibo-service` と `docs/PRIVACY.md` に向け直す（2026/09/30 完了）
- [ ] **楽天カードの店名入りの通知の形を実物で確認**（想定：`■利用先: ○○`、またはHTMLの表。家計簿の店名が「件名」や日付になっていたら直す）
- [ ] **GAS版からの引っ越しを実際の利用者（恵子さんなど）で試す**。つまずいた所を手順・コードに反映
- [ ] 家族にQRから友だち追加してもらい、「ようこそ」→連携→2〜3分で準備、の流れを実機で確認
- [ ] 家族以外に広げる前に、LINEトークン・Googleのクライアントシークレットを作り直す（手順：`docs/SECRETS_ROTATION.md`）

**10/04 の起動の変更（確認待ち）**
- [ ] サービス版：zip `kakeibo-service-update-20261004.zip`（`run.yml` の cron を `17 */2 * * *`・`gas/relay/Code.gs` に `kickRunner`）の **push が済んでいるか**（10/04 夕方の時点で GitHub は `d4efd57` のままだった）
- [ ] サービス版：中継役に貼り替え・新バージョン・`setupKickTrigger` 実行（晃介さんは「一応できた」）。⏰トリガーと Actions の「Manually run」で確認
- [ ] 専用版：起動役の `setup` は実行済み（ログ「✅ 30分おきの起動を設定し、試しに1回起動しました」）。Actions に「Manually run」が30分おきに並ぶか確認
- [ ] 専用版：10/04 夜の日次決算が届くか（届けば起動の変更は成功）

**専用版（kakeibo-python）の記録**（Claude Code からは直接読める）
- 2026/10/01 の Sheets の回数制限（429）→ `fix_quota.py`（サービス版で 9/25 に入れた「429で65秒待つ」「スプレッドシートを1回の実行で1回だけ開く」と同じもの）を適用・push 済み（`008aaec`）
- 毎月20日ごろ Actions が止まっていた原因：15分おきで月2,880回以上（非公開リポジトリの無料枠は月2,000分、1回最低1分）。
  10/01 に30分おき＋Node 24（`740007e`）→ 10/04 に schedule を `23 */6 * * *`（予備）に（`0cb2a88`）、起動は GAS の起動役から30分おき。
  1回20〜27秒なので、月 約1,440＋120回 ≒ 1,560分で枠内。60秒を超えるようになったら見直す
- 10/01 には Billing で止められていたが、10/02 以降は成功している
- [ ] `notify.py` の同一エラー1日1回の間引きを専用版にも移植（**Claude Code なら専用版を直接読めるので、もうできる**）

**利用者が増えたら**
- [ ] LINE有料プラン、状態で出し分けるリッチメニュー（連携前は2ボタン）、「お父様への支払日通知」のような追加の送り先
- 連携し直しの取り込み直しで、LINEの「重複の疑い」「重複を整理しました」通知（push）が出ることがある（1回の実行で最大1通ずつ）。気になったら取り込み直し中は止める
- `src/recategorize_all_transactions.py` はローカル用（自分の家計簿向け）の道具

## 7. 晃介さんとのやりとりの約束ごと（とても大事）

- **日本語・やさしい言葉**で、手順は番号付き・1ステップずつ。専門用語には一言説明
- **引き継ぎ書（このファイル）は、晃介さんが「引き継ぎ書を出して」と言ったときだけ作って見せる**（毎回出すとややこしいため。内容は覚えておき、出すときにまとめて反映）
- ファイルは**zipでまとめて渡し**、反映は次の形のコマンドで（ダウンロードフォルダの同名ファイルの取り違えを防ぐ）：
  ```bash
  cd ~/python/kakeibo-service
  unzip -o "$(ls -t ~/Downloads/kakeibo-service-update*.zip | head -1)"
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
- 変更は**Claude側で模擬環境のテストをしてから**渡す（GASはNodeでモック、Pythonは偽のスプレッドシート・偽のGmail）。pushやデプロイは全員に即反映されるため
- GitHub API（api.github.com）は共有のIPで回数制限（1時間60回）にすぐ当たる。当たったら `git clone https://github.com/kousukesuda1214-sys/<リポジトリ>.git` で読む（github.com は使える）
- 晃介さんがコマンドを実行した結果はスクショで届く。**「できた」と言われても、スクショの出力（`main -> main` が出ているか、`nothing to commit` や `no matches found` が無いか）を必ず確認する**
  （10/04 に、zip をダウンロードしていないまま進めて何も反映されていなかったことがあった）
- zshのプロンプトに複数行を貼ると、途中で切れて `for>` や `dquote>` で止まることがある → `control + C` で戻る。長いコマンドは1行にまとめる
