/**
 * 家計簿サービス 中継役（Google Apps Script）
 *
 * ■ この中継役がやること
 *  1. LINEで友だち追加・メッセージを受けたら、その人専用の「Googleと連携」リンクを返信する
 *  2. 連携ページ（GitHub Pages）から受け取った許可コードを「長く使える鍵」（refresh token）に交換し、
 *     このスクリプトの非公開の保管場所（スクリプトプロパティ）にだけ保存する。
 *     あわせて、本人のGoogleドライブに家計簿スプレッドシートを作る
 *  3. 晃介さんのPython（GitHub Actions）から合言葉付きで頼まれたら、利用者の一覧と、
 *     1時間で使えなくなる一時的な鍵（access token）だけを渡す。長く使える鍵は外に出さない
 *  4. ブロック（退会）されたら、または「連携解除」と送られたら、その人の鍵を削除し、
 *     Googleのアクセス許可も取り消す（家計簿のスプレッドシートは本人のドライブに残る）
 *  5. リッチメニュー（トーク画面の下の6つのボタン）が押されたら、Pythonが30分おきに預けていく
 *     「最新の数字」を使って、カード型（Flexメッセージ）で返信する
 *  6. LINEから設定（日次・週次決算の曜日、日次決算の時刻、高額アラートの金額）を変えられるようにする。
 *     設定の正しい値は、本人の家計簿の「設定」シートだけに持つ（LINEで変えたらシートに書き込み、
 *     「設定」ボタンを押したらシートをその場で読む）ので、LINEとシートで食い違わない
 *
 * ■ スクリプトプロパティ（プロジェクトの設定 → スクリプト プロパティ）に必要なもの
 *  LINE_CHANNEL_ACCESS_TOKEN … 日次決算Botのチャネルアクセストークン（長期）
 *  GOOGLE_CLIENT_ID          … Google Cloud「kakeibo-web」クライアントのID
 *  GOOGLE_CLIENT_SECRET      … 同クライアントのシークレット
 *  RUNNER_KEY                … Pythonとの合言葉（ランダムな長い文字列）
 *  GITHUB_TOKEN              … （任意）連携した直後に親玉をすぐ実行するための、GitHubの鍵。
 *                              kakeibo-service の「Actions：読み書き」だけを許可したもの。
 *                              無くても動く（その場合は30分おきの定期実行で準備される）
 *  ※ 利用者の鍵（user:〜）と連携リンクの控え（state:〜）も、ここに自動で保存される
 *
 * ■ 返信（reply）はLINEの無料枠を消費しない。push（こちらから送る）だけが月の通数に数えられる。
 *   そのため、友だち追加・メッセージへの応答はすべて返信で行う。
 */

// 連携ページのURL（Google Cloud の「承認済みのリダイレクトURI」と完全に一致させる）
const PAGE_URL = "https://kousukesuda1214-sys.github.io/kakeibo-auth/";
const REQUIRED_SCOPES = [
  "https://www.googleapis.com/auth/gmail.modify",
  "https://www.googleapis.com/auth/drive.file",
];
const STATE_TTL_MS = 60 * 60 * 1000; // 連携リンクの有効期限：1時間
const REQUIRED_PROPS = ["LINE_CHANNEL_ACCESS_TOKEN", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "RUNNER_KEY"];
const RUNNER_WORKFLOW_URL = "https://api.github.com/repos/kousukesuda1214-sys/kakeibo-service/actions/workflows/run.yml/dispatches";

const props_ = () => PropertiesService.getScriptProperties();
const prop_ = (name) => props_().getProperty(name);

// ============================================================
// 入口
// ============================================================

function doGet() {
  return HtmlService.createHtmlOutput("家計簿サービスの中継役は動いています。");
}

function doPost(e) {
  let body = {};
  try {
    body = JSON.parse((e.postData && e.postData.contents) || "{}");
  } catch (err) {
    return json_({ ok: false, error: "bad_request" });
  }

  try {
    // ① LINEのWebhook
    if (Array.isArray(body.events)) {
      body.events.forEach((ev) => {
        try {
          handleLineEvent_(ev);
        } catch (err) {
          console.error("LINEイベントの処理に失敗: " + err);
        }
      });
      return json_({ ok: true });
    }
    // ② 連携ページから（合言葉は不要。使い捨ての連携リンクの控えで本人確認する）
    if (body.action === "connect") {
      return json_(handleConnect_(String(body.code || ""), String(body.state || "")));
    }
    // ③ 晃介さんのPythonから（合言葉が必要）
    if (body.action) {
      if (!body.key || body.key !== prop_("RUNNER_KEY")) return json_({ ok: false, error: "unauthorized" });
      return json_(handleRunner_(body));
    }
    return json_({ ok: false, error: "unknown_request" });
  } catch (err) {
    console.error("処理に失敗: " + err);
    return json_({ ok: false, error: "internal_error" });
  }
}

// ============================================================
// LINE
// ============================================================

function handleLineEvent_(ev) {
  const userId = ev.source && ev.source.userId;
  if (!userId) return;

  if (ev.type === "follow") {
    // 友だち追加（ブロック解除も含む）への、最初のあいさつ。すでに連携済みの人（ブロック解除など）には、おかえりのあいさつ
    const user = getUser_(userId);
    reply_(ev.replyToken, [user ? welcomeBackCard_() : welcomeCard_(newConnectLink_(userId))]);
  } else if (ev.type === "unfollow") {
    handleUnfollow_(userId);
  } else if (ev.type === "message") {
    const text = ev.message && ev.message.type === "text" ? String(ev.message.text).trim() : "";
    reply_(ev.replyToken, messagesFor_(userId, text));
  } else if (ev.type === "postback") {
    reply_(ev.replyToken, postbackMessagesFor_(userId, String((ev.postback && ev.postback.data) || "")));
  }
}

/** 利用者から届いた言葉（リッチメニューのボタンを含む）に対する返信を作る。返信はLINEの月の通数を消費しない。 */
function messagesFor_(userId, text) {
  const user = getUser_(userId);

  if (text === "使い方") return [helpCard_(!!user)];

  if (!user) {
    return [
      "まだGoogleアカウントと連携されていません。\n下のリンクから連携してください（1時間有効）。\n" + newConnectLink_(userId),
    ];
  }

  if (text === "連携解除") {
    return [confirmDisconnect_()];
  }
  if (text === "連携解除する") {
    disconnectUser_(userId, user);
    return [
      "✅ Googleアカウントとの連携を解除しました。\n\n" +
        "・カード利用メールの読み取りと、家計簿への自動記録を止めました\n" +
        "・このシステムに預けていたGoogleの鍵は削除し、アクセス許可も取り消しました\n" +
        "・これまでの家計簿のスプレッドシートは、あなたのGoogleドライブにそのまま残っています\n\n" +
        "また使いたくなったら、何かメッセージを送ってください。連携のためのリンクが届きます。",
    ];
  }
  if (text === "やめる") return ["連携解除をやめました。これまでどおり家計簿を記録します。"];
  if (text === "連携・解除") return [connectionCard_(userId, user)];

  const snap = getSnapshot_(userId);
  if (!snap) {
    return [
      "⏳ 家計簿の準備中です。連携してから数分〜30分で準備が整います。\n" +
        "しばらくしてから、もう一度ボタンを押してください。",
    ];
  }
  if (text === "今日の決算") return [dailyCard_(snap)];
  if (text === "今月のカテゴリ別") return [categoryCard_(snap)];
  if (text === "家計簿を開く") return [sheetCard_(snap)];
  if (text === "設定") return [settingsCard_(userId, user, snap)];

  return [
    "下のメニューのボタンから、今日の決算や家計簿を見られます。\n" +
      "メニューが表示されていない場合は、画面下の「メニュー」をタップしてください。",
  ];
}

function handleUnfollow_(userId) {
  // なりすまし対策：本当にブロックされているかをLINEに確認する（友だちなら200が返る）
  const res = UrlFetchApp.fetch("https://api.line.me/v2/bot/profile/" + encodeURIComponent(userId), {
    headers: { Authorization: "Bearer " + prop_("LINE_CHANNEL_ACCESS_TOKEN") },
    muteHttpExceptions: true,
  });
  if (res.getResponseCode() === 200) return;

  const user = getUser_(userId);
  if (!user) return;
  disconnectUser_(userId, user);
  console.log("退会処理を行いました");
}

function disconnectUser_(userId, user) {
  revokeGoogleToken_(user.refresh_token);
  props_().deleteProperty("user:" + userId);
  props_().deleteProperty("snap:" + userId);
}

function reply_(replyToken, texts) {
  if (!replyToken) return;
  UrlFetchApp.fetch("https://api.line.me/v2/bot/message/reply", {
    method: "post",
    contentType: "application/json",
    headers: { Authorization: "Bearer " + prop_("LINE_CHANNEL_ACCESS_TOKEN") },
    payload: JSON.stringify({
      replyToken: replyToken,
      messages: texts.map((t) => (typeof t === "string" ? { type: "text", text: t } : t)),
    }),
    muteHttpExceptions: true,
  });
}

function push_(userId, text) {
  UrlFetchApp.fetch("https://api.line.me/v2/bot/message/push", {
    method: "post",
    contentType: "application/json",
    headers: { Authorization: "Bearer " + prop_("LINE_CHANNEL_ACCESS_TOKEN") },
    payload: JSON.stringify({ to: userId, messages: [{ type: "text", text: text }] }),
    muteHttpExceptions: true,
  });
}

// ============================================================
// 連携リンクと、Googleとの連携
// ============================================================

function newConnectLink_(userId) {
  cleanupStates_();
  const state = Utilities.getUuid();
  props_().setProperty("state:" + state, JSON.stringify({ userId: userId, exp: Date.now() + STATE_TTL_MS }));
  return PAGE_URL + "?s=" + state;
}

function cleanupStates_() {
  const all = props_().getProperties();
  const now = Date.now();
  Object.keys(all).forEach((k) => {
    if (k.indexOf("state:") !== 0) return;
    try {
      if (JSON.parse(all[k]).exp < now) props_().deleteProperty(k);
    } catch (err) {
      props_().deleteProperty(k);
    }
  });
}

function handleConnect_(code, state) {
  if (!code || !state) return { ok: false, message: "リンクが正しくありません。LINEで何かメッセージを送ると、新しいリンクが届きます。" };

  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    // 連携リンクの控えを確認し、すぐに捨てる（同じリンクは一度しか使えない）
    const raw = prop_("state:" + state);
    props_().deleteProperty("state:" + state);
    const saved = raw ? JSON.parse(raw) : null;
    if (!saved || saved.exp < Date.now()) {
      return { ok: false, message: "リンクの有効期限が切れています。LINEで何かメッセージを送ると、新しいリンクが届きます。" };
    }
    const userId = saved.userId;

    // 許可コードを鍵に交換する
    const res = UrlFetchApp.fetch("https://oauth2.googleapis.com/token", {
      method: "post",
      payload: {
        code: code,
        client_id: prop_("GOOGLE_CLIENT_ID"),
        client_secret: prop_("GOOGLE_CLIENT_SECRET"),
        redirect_uri: PAGE_URL,
        grant_type: "authorization_code",
      },
      muteHttpExceptions: true,
    });
    const token = JSON.parse(res.getContentText());
    if (res.getResponseCode() !== 200 || !token.refresh_token) {
      console.error("鍵への交換に失敗: " + res.getResponseCode() + " " + (token.error || ""));
      return { ok: false, message: "Googleとの連携に失敗しました。LINEで何かメッセージを送って、新しいリンクからやり直してください。" };
    }

    // 画面でチェックを外された権限が無いか確認する
    const granted = String(token.scope || "").split(" ");
    const missing = REQUIRED_SCOPES.filter((s) => granted.indexOf(s) === -1);
    if (missing.length) {
      revokeGoogleToken_(token.refresh_token);
      return {
        ok: false,
        message: "許可の画面で、チェックが外れている項目がありました。LINEで何かメッセージを送り、新しいリンクから、すべての項目にチェックを入れて許可してください。",
      };
    }

    const email = googleGet_("https://gmail.googleapis.com/gmail/v1/users/me/profile", token.access_token).emailAddress;

    // 連携し直しの場合は、古い鍵を無効にして、家計簿スプレッドシートはそのまま使う
    const old = getUser_(userId);
    if (old && old.refresh_token && old.refresh_token !== token.refresh_token) revokeGoogleToken_(old.refresh_token);
    let sheetId = old && old.email === email ? old.sheet_id : null;
    let sheetReady = old && old.email === email ? !!old.sheet_ready : false;

    if (!sheetId) {
      const created = googlePost_("https://sheets.googleapis.com/v4/spreadsheets", token.access_token, {
        properties: { title: "家計簿", locale: "ja_JP", timeZone: "Asia/Tokyo" },
      });
      sheetId = created.spreadsheetId;
      sheetReady = false;
    }

    saveUser_(userId, {
      refresh_token: token.refresh_token,
      email: email,
      sheet_id: sheetId,
      sheet_ready: sheetReady,
      connected_at: new Date().toISOString(),
    });
    console.log("連携が完了しました");

    // 新しい家計簿なら、30分おきの定期実行を待たずに、親玉を今すぐ動かして準備する
    const triggered = !sheetReady && triggerRunner_();

    return {
      ok: true,
      sheet_url: sheetUrl_(sheetId),
      message: sheetReady
        ? "連携し直しが完了しました。これまでの家計簿をそのまま使います。"
        : triggered
          ? "連携が完了しました！2〜3分で家計簿の準備が整い、LINEでお知らせが届きます。"
          : "連携が完了しました！30分以内に家計簿の準備が整い、LINEでお知らせが届きます。",
    };
  } finally {
    lock.releaseLock();
  }
}

// ============================================================
// 晃介さんのPythonからの依頼
// ============================================================

function handleRunner_(body) {
  if (body.action === "list_users") {
    const users = [];
    const all = props_().getProperties();
    Object.keys(all).forEach((k) => {
      if (k.indexOf("user:") !== 0) return;
      const userId = k.substring(5);
      const user = JSON.parse(all[k]);
      const accessToken = refreshAccessToken_(userId, user);
      if (!accessToken) return;
      users.push({
        user_id: userId,
        email: user.email,
        sheet_id: user.sheet_id,
        sheet_ready: !!user.sheet_ready,
        connected_at: user.connected_at, // 初回に取り込むメールの範囲を決めるのに使う
        access_token: accessToken, // 1時間で使えなくなる一時的な鍵だけを渡す
      });
    });
    return { ok: true, users: users };
  }

  if (body.action === "save_snapshot") {
    if (!getUser_(body.user_id)) return { ok: false, error: "not_found" };
    const raw = JSON.stringify(body.snapshot || {});
    if (raw.length > 8000) return { ok: false, error: "too_large" }; // スクリプトプロパティは1件9KBまで
    props_().setProperty("snap:" + body.user_id, raw);
    return { ok: true };
  }

  if (body.action === "mark_sheet_ready") {
    const user = getUser_(body.user_id);
    if (!user) return { ok: false, error: "not_found" };
    user.sheet_ready = true;
    saveUser_(body.user_id, user);
    return { ok: true };
  }

  return { ok: false, error: "unknown_action" };
}

/**
 * GitHubの親玉（kakeibo-service の run.yml）を今すぐ実行する。成功したらtrue。
 * 失敗しても問題はない（30分おきの定期実行で、同じように準備される）。
 */
function triggerRunner_() {
  const token = prop_("GITHUB_TOKEN");
  if (!token) return false;
  try {
    const res = UrlFetchApp.fetch(RUNNER_WORKFLOW_URL, {
      method: "post",
      contentType: "application/json",
      headers: { Authorization: "Bearer " + token, Accept: "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28" },
      payload: JSON.stringify({ ref: "main" }),
      muteHttpExceptions: true,
    });
    if (res.getResponseCode() === 204) return true;
    console.error("親玉の呼び出しに失敗: " + res.getResponseCode());
  } catch (err) {
    console.error("親玉の呼び出しに失敗: " + err);
  }
  return false;
}

function refreshAccessToken_(userId, user) {
  const res = UrlFetchApp.fetch("https://oauth2.googleapis.com/token", {
    method: "post",
    payload: {
      client_id: prop_("GOOGLE_CLIENT_ID"),
      client_secret: prop_("GOOGLE_CLIENT_SECRET"),
      refresh_token: user.refresh_token,
      grant_type: "refresh_token",
    },
    muteHttpExceptions: true,
  });
  const data = JSON.parse(res.getContentText());
  if (res.getResponseCode() === 200 && data.access_token) return data.access_token;

  if (data.error === "invalid_grant") {
    // 本人がGoogle側で許可を取り消した、パスワードを変えた など → 鍵を捨てて、連携し直しを案内する
    props_().deleteProperty("user:" + userId);
    props_().deleteProperty("snap:" + userId);
    push_(
      userId,
      "⚠️ Googleアカウントとの連携が切れたため、家計簿の自動記録を止めました。\n" +
        "このトークに何かメッセージを送ると、連携し直すためのリンクが届きます。"
    );
    console.log("連携切れの利用者を検出し、鍵を削除しました");
  } else {
    console.error("一時的な鍵の発行に失敗: " + res.getResponseCode() + " " + (data.error || ""));
  }
  return null;
}

// ============================================================
// カード型の返信（Flexメッセージ）
// ============================================================

const GREEN = "#06C755";
const DARK = "#1F4D3A";
const SUB = "#7A8C82";
const RED = "#E5484D";

function getSnapshot_(userId) {
  const raw = prop_("snap:" + userId);
  return raw ? JSON.parse(raw) : null;
}

function yen_(n) {
  const v = Math.round(Number(n) || 0);
  return (v < 0 ? "-" : "") + "¥" + String(Math.abs(v)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

function text_(t, opt) {
  return Object.assign({ type: "text", text: String(t), size: "sm", color: DARK, wrap: true }, opt || {});
}

function row_(label, value, opt) {
  return {
    type: "box", layout: "horizontal", margin: "md",
    contents: [
      text_(label, { color: SUB, flex: 5 }),
      text_(value, Object.assign({ align: "end", weight: "bold", flex: 5 }, opt || {})),
    ],
  };
}

function bar_(ratio, color) {
  const pct = Math.max(1, Math.min(100, Math.round(ratio * 100)));
  return {
    type: "box", layout: "vertical", height: "8px", backgroundColor: "#E6EFE9", cornerRadius: "4px", margin: "sm",
    contents: [{ type: "box", layout: "vertical", width: pct + "%", height: "8px", backgroundColor: color, cornerRadius: "4px", contents: [] }],
  };
}

function uriButton_(label, uri, primary) {
  return { type: "button", style: primary ? "primary" : "secondary", color: primary ? GREEN : undefined, height: "sm", margin: "sm", action: { type: "uri", label: label, uri: uri } };
}

function textButton_(label, text, primary) {
  return { type: "button", style: primary ? "primary" : "secondary", color: primary ? GREEN : undefined, height: "sm", margin: "sm", action: { type: "message", label: label, text: text } };
}

function card_(altText, title, subtitle, body, buttons) {
  const bubble = {
    type: "bubble",
    header: {
      type: "box", layout: "vertical", backgroundColor: "#F3FAF5", paddingAll: "16px",
      contents: [text_(title, { size: "lg", weight: "bold", color: DARK })].concat(subtitle ? [text_(subtitle, { size: "xs", color: SUB, margin: "xs" })] : []),
    },
    body: { type: "box", layout: "vertical", paddingAll: "16px", contents: body },
  };
  if (buttons && buttons.length) bubble.footer = { type: "box", layout: "vertical", paddingAll: "12px", contents: buttons };
  return { type: "flex", altText: altText, contents: bubble };
}

function sheetLink_(snap, gidKey) {
  const gid = gidKey && snap.gids ? snap.gids[gidKey] : null;
  return sheetUrl_(snap.sheet_id) + (gid !== null && gid !== undefined ? "#gid=" + gid : "");
}

function welcomeCard_(connectLink) {
  const step = (num, title, desc) => ({
    type: "box", layout: "horizontal", margin: "lg", spacing: "md",
    contents: [
      {
        type: "box", layout: "vertical", width: "28px", height: "28px", cornerRadius: "14px", backgroundColor: GREEN,
        justifyContent: "center", alignItems: "center", flex: 0,
        contents: [text_(num, { color: "#FFFFFF", weight: "bold", align: "center", size: "sm" })],
      },
      {
        type: "box", layout: "vertical", flex: 1,
        contents: [text_(title, { weight: "bold" }), text_(desc, { size: "xs", color: SUB, margin: "xs" })],
      },
    ],
  });
  return card_(
    "家計簿へようこそ！下のボタンからGoogleアカウントと連携してください。",
    "家計簿へようこそ！",
    "友だち追加ありがとうございます",
    [
      text_("カード会社からの利用通知メールを自動で読み取って、あなたのGoogleスプレッドシートに記録し、毎日の決算をこのLINEでお知らせします。", { color: SUB }),
      { type: "separator", margin: "lg" },
      step("1", "Googleと連携する", "下のボタンから、ご自分のGoogleアカウントで許可します"),
      step("2", "家計簿が自動でできる", "数分で、あなたのGoogleドライブに家計簿が作られます"),
      step("3", "毎日LINEでお知らせ", "決算が届きます。下のメニューからも、いつでも見られます"),
      text_("許可の画面で「Googleはこのアプリを確認していません」と出たら、「詳細」→「（アプリ名）に移動」で進んでください。", { size: "xxs", color: SUB, margin: "lg" }),
    ],
    [uriButton_("Googleと連携する", connectLink, true), text_("このボタンは1時間有効です。期限が切れたら、何かメッセージを送ってください。", { size: "xxs", color: SUB, align: "center", margin: "sm" })]
  );
}

function welcomeBackCard_() {
  return card_(
    "おかえりなさい！",
    "おかえりなさい！",
    "Googleアカウントとは連携したままです",
    [text_("これまでどおり、家計簿の記録とお知らせを続けます。下のメニューから、今日の決算や家計簿をいつでも見られます。", { color: SUB })],
    [textButton_("今日の決算を見る", "今日の決算", true)]
  );
}

function dailyCard_(s) {
  const ratio = s.budget > 0 ? s.total / s.budget : 0;
  let alert = null;
  if (ratio >= 1.2) alert = "🔴 予算を大幅にオーバーしています";
  else if (ratio >= 1.0) alert = "⚠️ 予算をオーバーしています";
  else if (ratio >= 0.8) alert = "🟡 予算の80%を使いました";

  const pace = s.budget_to_date - s.total;
  const body = [];
  if (alert) body.push(text_(alert, { weight: "bold", color: ratio >= 1 ? RED : DARK, margin: "none" }));
  body.push(text_("締め期間の累計", { size: "xs", color: SUB, margin: alert ? "md" : "none" }));
  body.push({
    type: "box", layout: "baseline", margin: "xs",
    contents: [
      text_(yen_(s.total), { size: "xxl", weight: "bold", color: ratio >= 1 ? RED : DARK, flex: 0 }),
      text_(" ／ 予算 " + yen_(s.budget), { size: "sm", color: SUB, margin: "sm" }),
    ],
  });
  body.push(bar_(ratio, ratio >= 1 ? RED : GREEN));
  body.push({ type: "separator", margin: "lg" });
  body.push(row_("💳 本日", yen_(s.today)));
  body.push(row_("🎯 今日までの目安", yen_(s.budget_to_date)));
  body.push(row_("📊 ペース", pace >= 0 ? yen_(pace) + " 余裕" : yen_(-pace) + " 押し気味", { color: pace >= 0 ? GREEN : RED }));
  body.push(row_(s.remaining >= 0 ? "💰 締めまで使える" : "💸 予算超過", yen_(Math.abs(s.remaining)), { color: s.remaining >= 0 ? DARK : RED }));
  body.push(row_("📈 締め時点の予測", yen_(s.forecast)));
  if (s.budget_is_sample) {
    body.push(text_("※「予算計画」シートに予算が未入力のため、仮の予算で計算しています", { size: "xxs", color: SUB, margin: "lg" }));
  }
  return card_(
    "今日の決算：累計 " + yen_(s.total) + " ／ 予算 " + yen_(s.budget),
    "今日の決算",
    s.period + "（" + s.day_index + "/" + s.days + "日目）・" + s.updated_at + "時点",
    body,
    [uriButton_("家計簿を開く", sheetLink_(s, "budget"), true)]
  );
}

function categoryCard_(s) {
  const total = s.categories.reduce((a, c) => a + Math.max(c.amount, 0), 0);
  const body = [];
  if (!s.categories.length) {
    body.push(text_("この締め期間の利用は、まだありません。", { color: SUB }));
  }
  s.categories.forEach((c, i) => {
    const ratio = total > 0 ? Math.max(c.amount, 0) / total : 0;
    body.push({
      type: "box", layout: "vertical", margin: i === 0 ? "none" : "lg",
      contents: [
        {
          type: "box", layout: "horizontal",
          contents: [
            text_(c.name, { flex: 6 }),
            text_(yen_(c.amount), { align: "end", weight: "bold", flex: 4 }),
            text_(Math.round(ratio * 100) + "%", { align: "end", size: "xs", color: SUB, flex: 2, gravity: "center" }),
          ],
        },
        bar_(ratio, GREEN),
      ],
    });
  });
  return card_(
    "今月のカテゴリ別：合計 " + yen_(s.total),
    "今月のカテゴリ別",
    s.period + "・" + s.count + "件・" + s.updated_at + "時点",
    body,
    [uriButton_("取引明細を見る", sheetLink_(s, "detail"), true), uriButton_("分類のルールを変える", sheetLink_(s, "category_rules"), false)]
  );
}

function sheetCard_(s) {
  return card_(
    "家計簿を開く",
    "家計簿を開く",
    "あなたのGoogleドライブにある家計簿です",
    [text_("見たいシートを選んでください。スプレッドシートのアプリが入っていれば、アプリで開きます。", { color: SUB })],
    [
      uriButton_("予算計画", sheetLink_(s, "budget"), true),
      uriButton_("取引明細", sheetLink_(s, "detail"), false),
      uriButton_("設定", sheetLink_(s, "settings"), false),
    ]
  );
}

// ============================================================
// LINEから設定を変える（本人の家計簿の「設定」シートを、その場で読み書きする）
// ============================================================

const WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"];
const FULL_WEEK = 127; // 7曜日すべて（ビットの並び：月=1, 火=2, 水=4, … 日=64）

// LINEから変えられる設定。label は「設定」シートのB列の項目名と同じにする（Python の settings.py と揃える）
const EDITABLE = {
  daily_days: { label: "日次決算を送る曜日", type: "days", allowNone: false },
  weekly_days: { label: "週次決算を送る曜日", type: "days", allowNone: true },
  daily_hour: { label: "日次決算を送る時刻", type: "hour" },
  alert_amount: { label: "高額利用アラートの金額", type: "amount" },
};
const EDITABLE_BY_LABEL = {};
Object.keys(EDITABLE).forEach((k) => { EDITABLE_BY_LABEL[EDITABLE[k].label] = k; });
const HOURS = [6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23];
const AMOUNTS = [5000, 10000, 20000, 30000, 50000, 100000];

/** 「月・木」「平日」「毎日」「送らない」などを、曜日のビットの組にする（読めなければ null）。Python の settings.py と同じ読み方 */
function parseDays_(raw) {
  const t = String(raw || "").trim().replace(/[０-９]/g, (c) => String.fromCharCode(c.charCodeAt(0) - 0xfee0));
  if (!t) return null;
  if (t === "毎日" || t === "毎日送る") return FULL_WEEK;
  if (["送らない", "なし", "無し", "しない", "オフ", "off", "OFF"].indexOf(t) !== -1) return 0;
  let mask = 0;
  const tokens = t.split(/[・、,，\/／\s　と]+/).filter((x) => x);
  for (let i = 0; i < tokens.length; i++) {
    const tok = tokens[i];
    if (tok === "平日") mask |= 31;
    else if (tok === "週末" || tok === "土日") mask |= 96;
    else if (/^[1-7]$/.test(tok)) mask |= 1 << (Number(tok) - 1);
    else if (WEEKDAYS.indexOf(tok.charAt(0)) !== -1) mask |= 1 << WEEKDAYS.indexOf(tok.charAt(0));
    else return null;
  }
  return mask;
}

function showDays_(mask) {
  if (mask === 0) return "送らない";
  if (mask === FULL_WEEK) return "毎日";
  if (mask === 31) return "平日";
  if (mask === 96) return "週末";
  return WEEKDAYS.filter((_, i) => mask & (1 << i)).join("・");
}

function countDays_(mask) {
  let n = 0;
  for (let i = 0; i < 7; i++) if (mask & (1 << i)) n++;
  return n;
}

/** 本人の家計簿の「設定」シートを読む。{ 項目名: { row: 行番号, value: C列の値 } }（読めなければ null） */
function readSettingsSheet_(userId, user) {
  try {
    return readSettingsSheetOrThrow_(userId, user);
  } catch (err) {
    // 通信の一時的な失敗など。呼び出し側は、預かっている最新の数字の中の設定で代わりに表示する
    console.error("設定シートの読み取りに失敗: " + err);
    return null;
  }
}

function readSettingsSheetOrThrow_(userId, user) {
  const token = refreshAccessToken_(userId, user);
  if (!token) return null;
  const range = encodeURIComponent("設定!B3:C40");
  const res = UrlFetchApp.fetch("https://sheets.googleapis.com/v4/spreadsheets/" + user.sheet_id + "/values/" + range, {
    headers: { Authorization: "Bearer " + token }, muteHttpExceptions: true,
  });
  if (res.getResponseCode() !== 200) return null;
  const rows = JSON.parse(res.getContentText()).values || [];
  const settings = { __token: token };
  rows.forEach((r, i) => {
    const label = String(r[0] || "").trim();
    if (label) settings[label] = { row: i + 3, value: String(r[1] === undefined ? "" : r[1]) };
  });
  return settings;
}

/** 本人の家計簿の「設定」シートの、指定した行のC列に書き込む */
function writeSettingsCell_(user, token, row, value) {
  try {
    return writeSettingsCellOrThrow_(user, token, row, value);
  } catch (err) {
    console.error("設定シートへの書き込みに失敗: " + err);
    return false;
  }
}

function writeSettingsCellOrThrow_(user, token, row, value) {
  const range = encodeURIComponent("設定!C" + row);
  const res = UrlFetchApp.fetch(
    "https://sheets.googleapis.com/v4/spreadsheets/" + user.sheet_id + "/values/" + range + "?valueInputOption=RAW",
    {
      method: "put", contentType: "application/json", headers: { Authorization: "Bearer " + token },
      payload: JSON.stringify({ values: [[value]] }), muteHttpExceptions: true,
    }
  );
  return res.getResponseCode() === 200;
}

function chip_(label, selected, data) {
  return {
    type: "box", layout: "vertical", flex: 1, paddingAll: "8px", cornerRadius: "8px",
    backgroundColor: selected ? GREEN : "#EEF3EF",
    action: { type: "postback", label: label.substring(0, 20), data: data },
    contents: [text_(label, { align: "center", size: "sm", weight: "bold", color: selected ? "#FFFFFF" : DARK, wrap: false })],
  };
}

function chipRows_(chips, perRow) {
  const rows = [];
  for (let i = 0; i < chips.length; i += perRow) {
    const row = chips.slice(i, i + perRow);
    while (row.length < perRow) row.push({ type: "box", layout: "vertical", flex: 1, contents: [] }); // 空きマス
    rows.push({ type: "box", layout: "horizontal", spacing: "sm", margin: "sm", contents: row });
  }
  return rows;
}

/** 設定の一覧（シートをその場で読む。読めなければ、30分おきに預かっている最新の数字の中の設定を使う） */
function settingsCard_(userId, user, snap) {
  const sheet = readSettingsSheet_(userId, user);
  const items = sheet
    ? Object.keys(sheet).filter((k) => k !== "__token").map((label) => ({ label: label, value: sheet[label].value }))
    : (snap && snap.settings) || [];

  const body = items.map((x, i) => {
    const key = EDITABLE_BY_LABEL[x.label];
    const contents = [
      text_(x.label, { color: SUB, flex: 5, size: "xs" }),
      text_(x.value || "（空欄）", { align: "end", weight: "bold", flex: 4, size: "sm" }),
    ];
    contents.push(key
      ? { type: "box", layout: "vertical", flex: 2, action: { type: "postback", label: "変更", data: "cfg|" + key + "|edit" },
          contents: [text_("変更 ›", { align: "end", color: GREEN, weight: "bold", size: "sm" })] }
      : { type: "box", layout: "vertical", flex: 2, contents: [] });
    return { type: "box", layout: "horizontal", margin: i === 0 ? "none" : "md", contents: contents };
  });
  body.push(text_("「変更 ›」の無い項目は、「設定」シートのC列で変えられます。どちらで変えても、次回の自動実行（30分以内）から反映されます。",
    { size: "xxs", color: SUB, margin: "lg" }));
  const link = snap ? sheetLink_(snap, "settings") : sheetUrl_(user.sheet_id);
  return card_("現在の設定", "現在の設定", sheet ? "家計簿の「設定」シートの内容です" : (snap ? snap.updated_at + "時点" : ""), body,
    [uriButton_("スプレッドシートで変える", link, false)]);
}

function daysEditor_(key, mask, note) {
  const def = EDITABLE[key];
  const dayChips = WEEKDAYS.map((d, i) => chip_(d, !!(mask & (1 << i)), "cfg|" + key + "|toggle|" + (mask ^ (1 << i))));
  const presets = [["毎日", FULL_WEEK], ["平日", 31], ["週末", 96]];
  if (def.allowNone) presets.push(["送らない", 0]);
  const presetChips = presets.map((p) => chip_(p[0], mask === p[1], "cfg|" + key + "|toggle|" + p[1]));
  const body = [text_("タップして選び、最後に「保存」を押してください。", { size: "xs", color: SUB })]
    .concat(chipRows_(dayChips, 7))
    .concat([text_("まとめて選ぶ", { size: "xxs", color: SUB, margin: "lg" })])
    .concat(chipRows_(presetChips, 4))
    .concat([
      { type: "separator", margin: "lg" },
      text_("選択中：" + showDays_(mask) + (mask ? "（週" + countDays_(mask) + "回）" : ""), { weight: "bold", margin: "md" }),
    ]);
  if (note) body.push(text_(note, { size: "xs", color: RED, margin: "sm" }));
  return card_(def.label + "を選ぶ", def.label, "LINEで変えると、家計簿の「設定」シートにも書き込みます", body, [
    { type: "button", style: "primary", color: GREEN, height: "sm", margin: "sm",
      action: { type: "postback", label: "保存する", data: "cfg|" + key + "|save|" + mask, displayText: def.label + "を「" + showDays_(mask) + "」にする" } },
  ]);
}

function choiceEditor_(key, current) {
  const def = EDITABLE[key];
  let chips;
  if (def.type === "hour") {
    chips = HOURS.map((h) => chip_(h + "時", String(current) === String(h), "cfg|" + key + "|save|" + h));
  } else {
    chips = AMOUNTS.map((a) => chip_(yen_(a).replace("¥", "") + "円", String(current).replace(/[,円¥\s]/g, "") === String(a), "cfg|" + key + "|save|" + a));
  }
  const body = [text_(def.type === "hour" ? "決算を届けてほしい時刻をタップしてください（最大30分ほど遅れることがあります）。" :
    "この金額以上の利用があったら、すぐにLINEでお知らせします。", { size: "xs", color: SUB })]
    .concat(chipRows_(chips, def.type === "hour" ? 6 : 3));
  return card_(def.label + "を選ぶ", def.label, "今の設定：" + (current || "（空欄）"), body, []);
}

/** 設定まわりのボタン（postback）への返信を作る */
function postbackMessagesFor_(userId, data) {
  const user = getUser_(userId);
  if (!user) return ["まだGoogleアカウントと連携されていません。\n下のリンクから連携してください（1時間有効）。\n" + newConnectLink_(userId)];
  const parts = data.split("|");
  if (parts[0] !== "cfg" || !EDITABLE[parts[1]]) return [];
  const key = parts[1], op = parts[2], arg = parts[3], def = EDITABLE[key];

  if (op === "edit") {
    const sheet = readSettingsSheet_(userId, user);
    const current = sheet && sheet[def.label] ? sheet[def.label].value : "";
    if (def.type === "days") {
      const mask = parseDays_(current);
      return [daysEditor_(key, mask === null ? FULL_WEEK : mask)];
    }
    return [choiceEditor_(key, current)];
  }
  if (op === "toggle") return [daysEditor_(key, Number(arg) & FULL_WEEK)];

  if (op === "save") {
    let value;
    if (def.type === "days") {
      const mask = Number(arg) & FULL_WEEK;
      if (!mask && !def.allowNone) return [daysEditor_(key, mask, "日次決算は、週1回以上にしてください")];
      value = showDays_(mask);
    } else if (def.type === "hour") {
      if (HOURS.indexOf(Number(arg)) === -1) return [];
      value = String(Number(arg));
    } else {
      if (AMOUNTS.indexOf(Number(arg)) === -1) return [];
      value = String(Number(arg));
    }
    const sheet = readSettingsSheet_(userId, user);
    if (!sheet || !sheet[def.label]) {
      return ["⚠️ 家計簿の「設定」シートの準備が、まだ整っていません。30分ほどしてから、もう一度お試しください。"];
    }
    if (!writeSettingsCell_(user, sheet.__token, sheet[def.label].row, value)) {
      return ["⚠️ 家計簿に書き込めませんでした。少し時間をおいて、もう一度お試しください。"];
    }
    const shown = def.type === "hour" ? value + "時" : def.type === "amount" ? yen_(value).replace("¥", "") + "円" : value;
    return [
      "✅ " + def.label + "を「" + shown + "」にしました。\n家計簿の「設定」シートにも書き込みました。次回の自動実行（30分以内）から反映されます。",
    ];
  }
  return [];
}

function helpCard_(connected) {
  const items = [
    ["📊 今日の決算", "締め期間の累計・予算との比較を、いつでも見られます"],
    ["🔔 自動のお知らせ", "毎日の決算・週1回の内訳・高額な利用を、このLINEでお知らせします"],
    ["📒 家計簿", "カードの利用は、あなたのGoogleドライブのスプレッドシートに自動で記録されます"],
    ["⚙️ 設定", "締め日・通知の時刻などは「設定」シートで変えられます"],
    ["🏷️ 分類", "カテゴリは自動で判定します。「カテゴリルール」シートで自分好みに直せます"],
  ];
  const body = [];
  items.forEach((it, i) => {
    body.push(text_(it[0], { weight: "bold", margin: i === 0 ? "none" : "lg" }));
    body.push(text_(it[1], { size: "xs", color: SUB, margin: "xs" }));
  });
  body.push({ type: "separator", margin: "lg" });
  body.push(text_("記録されないときは：「送信元リスト」シートに、お使いのカード会社の通知メールのアドレスがあるか確認してください。", { size: "xs", color: SUB, margin: "lg" }));
  return card_("使い方", "使い方", "家計簿サービスでできること", body, connected ? [] : [textButton_("Googleと連携する", "連携する", true)]);
}

function connectionCard_(userId, user) {
  return card_(
    "連携・解除",
    "Googleアカウントの連携",
    "✅ 連携中",
    [
      row_("アカウント", user.email || "", { size: "xs" }),
      row_("連携した日", String(user.connected_at || "").substring(0, 10)),
      text_("別のGoogleアカウントに切り替えるときは「連携し直す」、使うのをやめるときは「連携を解除する」を押してください。", { size: "xs", color: SUB, margin: "lg" }),
    ],
    [uriButton_("連携し直す（1時間有効）", newConnectLink_(userId), false), textButton_("連携を解除する", "連携解除", false)]
  );
}

function confirmDisconnect_() {
  return {
    type: "template",
    altText: "連携を解除しますか？",
    template: {
      type: "confirm",
      text: "Googleアカウントとの連携を解除しますか？\n（家計簿のスプレッドシートは残ります）",
      actions: [
        { type: "message", label: "解除する", text: "連携解除する" },
        { type: "message", label: "やめる", text: "やめる" },
      ],
    },
  };
}

// ============================================================
// リッチメニュー（晃介さんが1回だけ手動で実行する）
// ============================================================

const RICH_MENU_IMAGE_URL = "https://raw.githubusercontent.com/kousukesuda1214-sys/kakeibo-auth/main/richmenu.png";

/**
 * トーク画面の下の6つのボタン（リッチメニュー）を登録し、全員のデフォルトにする。
 * 画像を差し替えたときも、これをもう一度実行すればよい（古いメニューは自動で消す）。
 */
function setupRichMenu() {
  const token = prop_("LINE_CHANNEL_ACCESS_TOKEN");
  const auth = { Authorization: "Bearer " + token };
  const cols = [0, 833, 1667, 2500];
  const rows = [0, 843, 1686];
  const labels = ["今日の決算", "家計簿を開く", "設定", "今月のカテゴリ別", "使い方", "連携・解除"];
  const areas = labels.map((label, i) => {
    const c = i % 3, r = Math.floor(i / 3);
    return {
      bounds: { x: cols[c], y: rows[r], width: cols[c + 1] - cols[c], height: rows[r + 1] - rows[r] },
      action: { type: "message", label: label, text: label },
    };
  });

  // 1. メニューの枠（ボタンの位置と動き）を作る
  const created = UrlFetchApp.fetch("https://api.line.me/v2/bot/richmenu", {
    method: "post", contentType: "application/json", headers: auth, muteHttpExceptions: true,
    payload: JSON.stringify({
      size: { width: 2500, height: 1686 }, selected: true, name: "家計簿メニュー", chatBarText: "メニュー", areas: areas,
    }),
  });
  if (created.getResponseCode() !== 200) throw new Error("メニューの作成に失敗：" + created.getContentText());
  const richMenuId = JSON.parse(created.getContentText()).richMenuId;

  // 2. 画像をGitHubから取ってきて、メニューに貼る
  const image = UrlFetchApp.fetch(RICH_MENU_IMAGE_URL).getBlob();
  const uploaded = UrlFetchApp.fetch("https://api-data.line.me/v2/bot/richmenu/" + richMenuId + "/content", {
    method: "post", contentType: "image/png", headers: auth, payload: image.getBytes(), muteHttpExceptions: true,
  });
  if (uploaded.getResponseCode() !== 200) throw new Error("画像の登録に失敗：" + uploaded.getContentText());

  // 3. 全員のデフォルトのメニューにする
  const def = UrlFetchApp.fetch("https://api.line.me/v2/bot/user/all/richmenu/" + richMenuId, {
    method: "post", headers: auth, muteHttpExceptions: true,
  });
  if (def.getResponseCode() !== 200) throw new Error("デフォルトへの設定に失敗：" + def.getContentText());

  // 4. 以前に登録した古いメニューを消す
  const list = JSON.parse(UrlFetchApp.fetch("https://api.line.me/v2/bot/richmenu/list", { headers: auth }).getContentText());
  (list.richmenus || []).forEach((m) => {
    if (m.richMenuId !== richMenuId && m.name === "家計簿メニュー") {
      UrlFetchApp.fetch("https://api.line.me/v2/bot/richmenu/" + m.richMenuId, { method: "delete", headers: auth, muteHttpExceptions: true });
    }
  });
  console.log("✅ リッチメニューを登録しました（" + richMenuId + "）");
}

// ============================================================
// 共通
// ============================================================

function getUser_(userId) {
  const raw = prop_("user:" + userId);
  return raw ? JSON.parse(raw) : null;
}

function saveUser_(userId, user) {
  props_().setProperty("user:" + userId, JSON.stringify(user));
}

function revokeGoogleToken_(refreshToken) {
  if (!refreshToken) return;
  UrlFetchApp.fetch("https://oauth2.googleapis.com/revoke", {
    method: "post",
    payload: { token: refreshToken },
    muteHttpExceptions: true,
  });
}

function googleGet_(url, accessToken) {
  const res = UrlFetchApp.fetch(url, { headers: { Authorization: "Bearer " + accessToken }, muteHttpExceptions: true });
  if (res.getResponseCode() !== 200) throw new Error("Google API エラー " + res.getResponseCode());
  return JSON.parse(res.getContentText());
}

function googlePost_(url, accessToken, payload) {
  const res = UrlFetchApp.fetch(url, {
    method: "post",
    contentType: "application/json",
    headers: { Authorization: "Bearer " + accessToken },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true,
  });
  if (res.getResponseCode() !== 200) throw new Error("Google API エラー " + res.getResponseCode());
  return JSON.parse(res.getContentText());
}

function sheetUrl_(sheetId) {
  return "https://docs.google.com/spreadsheets/d/" + sheetId + "/edit";
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

// ============================================================
// 晃介さんが手動で実行する確認用の関数（エディタ上部で選んで「実行」）
// ============================================================

/** スクリプトプロパティが揃っているか、利用者が何人いるかを確認する（値そのものは表示しない） */
function checkSetup() {
  const all = props_().getProperties();
  REQUIRED_PROPS.forEach((name) => console.log((all[name] ? "✅ " : "❌ 未設定：") + name));
  console.log((all.GITHUB_TOKEN ? "✅ " : "➖ 未設定（任意）：") + "GITHUB_TOKEN");
  const users = Object.keys(all).filter((k) => k.indexOf("user:") === 0).length;
  const states = Object.keys(all).filter((k) => k.indexOf("state:") === 0).length;
  console.log("連携済みの利用者：" + users + "人 ／ 有効な連携リンク：" + states + "件");
}
