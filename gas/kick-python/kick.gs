/**
 * 家計簿 専用版 起動役
 * 30分おきに、GitHub Actions の kakeibo-python（daily.yml）を「手動実行」として起動する。
 * GitHub の定期実行（schedule）は混雑時に飛ばされることがあるが、手動実行は飛ばされないため。
 *
 * スクリプト プロパティ：GITHUB_TOKEN（kakeibo-python の Actions を「読み書き」できるトークン）
 * 最初に1回だけ setup を実行する（30分おきのトリガーを作り、試しに1回起動する）。
 * 起動に失敗すると、Google からエラーのメールが届く。
 */
const REPO = "kousukesuda1214-sys/kakeibo-python";
const WORKFLOW = "daily.yml";

function kick() {
  const token = PropertiesService.getScriptProperties().getProperty("GITHUB_TOKEN");
  if (!token) throw new Error("スクリプト プロパティに GITHUB_TOKEN がありません");
  const res = UrlFetchApp.fetch(
    "https://api.github.com/repos/" + REPO + "/actions/workflows/" + WORKFLOW + "/dispatches",
    {
      method: "post",
      contentType: "application/json",
      headers: {
        Authorization: "Bearer " + token,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
      },
      payload: JSON.stringify({ ref: "main" }),
      muteHttpExceptions: true,
    }
  );
  const code = res.getResponseCode();
  if (code !== 204 && code !== 200) {
    throw new Error("起動に失敗しました（" + code + "）：" + res.getContentText().slice(0, 200));
  }
  console.log("起動しました");
}

function setup() {
  ScriptApp.getProjectTriggers()
    .filter((t) => t.getHandlerFunction() === "kick")
    .forEach((t) => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger("kick").timeBased().everyMinutes(30).create();
  kick();
  console.log("✅ 30分おきの起動を設定し、試しに1回起動しました");
}
