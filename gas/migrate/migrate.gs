/**
 * 家計簿の引っ越し（GAS版 → 新しい家計簿サービス）
 *
 * ■ 使い方
 *  1. 下の NEW_KAKEIBO_URL の "" の中に、新しい家計簿のURLを貼る
 *     （LINEの日次決算Botの「家計簿を開く」→ 開いた家計簿のアドレスバーのURL）
 *  2. 上の関数の選択欄で「migrateToNewKakeibo」を選んで「実行」を押す
 *  3. 「承認が必要です」と出たら、自分のアカウントで許可する
 *  4. 下の「実行ログ」に「✅ 引っ越しが完了しました」と出たら終わり
 *
 * ■ このコードがやること
 *  - 取引明細・ログ：新しい家計簿が自動で取り込んだ期間より「前」の分だけをコピーする（重複しない）
 *  - 予算計画：月ごとの予算・実績・引落し確定額をコピーする（同じ月は、この家計簿の値を優先）
 *  - カテゴリ予算・送信元リスト：この家計簿の内容で置き換える
 *  - この家計簿の自動実行（トリガー）を止める（同じ通知が2回届かないように）
 *  - 何回実行しても大丈夫（コピー済みの分は、2回目以降は飛ばす）
 *
 * この家計簿（GAS版）の中身は、何も変更・削除しません。
 */

const NEW_KAKEIBO_URL = "";

function migrateToNewKakeibo() {
  if (!NEW_KAKEIBO_URL) throw new Error("1行目の NEW_KAKEIBO_URL に、新しい家計簿のURLを貼ってから実行してください。");
  const oldSs = SpreadsheetApp.getActiveSpreadsheet();
  const newSs = SpreadsheetApp.openByUrl(NEW_KAKEIBO_URL);
  if (oldSs.getId() === newSs.getId()) throw new Error("URLが、この家計簿自身になっています。新しい家計簿のURLを貼ってください。");
  const tz = oldSs.getSpreadsheetTimeZone();

  for (const name of ["取引明細", "ログ", "予算計画"]) {
    if (!newSs.getSheetByName(name)) {
      throw new Error("新しい家計簿に「" + name + "」シートがまだありません。連携してから数分待って、もう一度実行してください。");
    }
  }

  console.log("① 取引明細：" + copyDetail_(oldSs, newSs, tz));
  console.log("② ログ：" + copyLog_(oldSs, newSs, tz));
  console.log("③ 予算計画：" + mergeBudget_(oldSs, newSs));
  console.log("④ カテゴリ予算：" + replaceTable_(oldSs, newSs, "カテゴリ予算"));
  console.log("⑤ 送信元リスト：" + replaceTable_(oldSs, newSs, "送信元リスト"));
  console.log("⑥ この家計簿の自動実行：" + stopTriggers_());
  console.log(
    "✅ 引っ越しが完了しました。\n" +
      "締め日・通知の時刻などは、LINEの「設定」ボタンか、新しい家計簿の「設定」シートで合わせてください。\n" +
      "新しい家計簿の並べ替え・累計の計算は、次の自動実行（30分以内）で整います。"
  );
}

// ============================================================
// 共通
// ============================================================

/** シートの中から、指定した見出しの文字が並んでいる行を探す。{ row: 行番号(1始まり), cols: {見出し: 列番号(0始まり)} } */
function findHeader_(values, names) {
  for (let r = 0; r < Math.min(values.length, 10); r++) {
    const cols = {};
    values[r].forEach((v, c) => { const t = String(v).trim(); if (names.indexOf(t) !== -1 && cols[t] === undefined) cols[t] = c; });
    if (Object.keys(cols).length === names.length) return { row: r + 1, cols: cols };
  }
  return null;
}

/** 日付のセル（Date でも文字でも）を Date にする。読めなければ null */
function toDate_(v) {
  if (v instanceof Date) return isNaN(v.getTime()) ? null : v;
  const m = String(v).trim().match(/^(\d{4})[\/\-](\d{1,2})[\/\-](\d{1,2})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?/);
  if (!m) return null;
  return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), Number(m[4] || 0), Number(m[5] || 0), Number(m[6] || 0));
}

function dayKey_(d, tz) {
  return Utilities.formatDate(d, tz, "yyyy/MM/dd");
}

/** 新しい家計簿の最終行の下に、行をまとめて書き込む（日付の列は、Pythonが読めるよう文字として書く） */
function appendRows_(sheet, startCol, rows, textCols) {
  if (!rows.length) return;
  const start = sheet.getLastRow() + 1;
  const need = start + rows.length - 1;
  if (sheet.getMaxRows() < need) sheet.insertRowsAfter(sheet.getMaxRows(), need - sheet.getMaxRows());
  (textCols || []).forEach((c) => sheet.getRange(start, startCol + c, rows.length, 1).setNumberFormat("@"));
  sheet.getRange(start, startCol, rows.length, rows[0].length).setValues(rows);
}

// ============================================================
// 取引明細・ログ
// ============================================================

function copyDetail_(oldSs, newSs, tz) {
  const names = ["日時", "店舗名", "金額", "カテゴリ"];
  const oldValues = oldSs.getSheetByName("取引明細").getDataRange().getValues();
  const newSheet = newSs.getSheetByName("取引明細");
  const newValues = newSheet.getDataRange().getValues();
  const oh = findHeader_(oldValues, names), nh = findHeader_(newValues, names);
  if (!oh || !nh) return "見出し（日時・店舗名・金額・カテゴリ）が見つからないため、飛ばしました";

  // 新しい家計簿に、すでに入っている一番古い日（これより前の分だけをコピーする）
  let firstDay = null;
  newValues.slice(nh.row).forEach((r) => {
    const d = toDate_(r[nh.cols["日時"]]);
    if (d && (!firstDay || dayKey_(d, tz) < firstDay)) firstDay = dayKey_(d, tz);
  });

  const rows = [];
  oldValues.slice(oh.row).forEach((r) => {
    const d = toDate_(r[oh.cols["日時"]]);
    if (!d || r[oh.cols["金額"]] === "" || r[oh.cols["金額"]] === null) return;
    if (firstDay && dayKey_(d, tz) >= firstDay) return;
    rows.push([Utilities.formatDate(d, tz, "yyyy/MM/dd HH:mm:ss"), r[oh.cols["店舗名"]], r[oh.cols["金額"]], r[oh.cols["カテゴリ"]]]);
  });
  appendRows_(newSheet, nh.cols["日時"] + 1, rows, [0]);
  return rows.length + "件をコピーしました" + (firstDay ? "（" + firstDay + " より前の分）" : "");
}

function copyLog_(oldSs, newSs, tz) {
  const oldValues = oldSs.getSheetByName("ログ").getDataRange().getValues();
  const newSheet = newSs.getSheetByName("ログ");
  const newValues = newSheet.getDataRange().getValues();
  const pick = (values) => findHeader_(values, ["日時", "金額"]) || findHeader_(values, ["日付", "金額"]);
  const oh = pick(oldValues), nh = pick(newValues);
  if (!oh || !nh) return "見出し（日時・金額）が見つからないため、飛ばしました";
  const oDate = oh.cols["日時"] !== undefined ? oh.cols["日時"] : oh.cols["日付"];
  const nDate = nh.cols["日時"] !== undefined ? nh.cols["日時"] : nh.cols["日付"];

  const existing = {};
  let firstDay = null;
  newValues.slice(nh.row).forEach((r) => {
    const d = toDate_(r[nDate]);
    if (!d) return;
    const k = dayKey_(d, tz);
    existing[k] = true;
    if (!firstDay || k < firstDay) firstDay = k;
  });

  const rows = [];
  oldValues.slice(oh.row).forEach((r) => {
    const d = toDate_(r[oDate]);
    if (!d) return;
    const k = dayKey_(d, tz);
    if (existing[k] || (firstDay && k >= firstDay)) return;
    rows.push([k, r[oh.cols["金額"]]]);
  });
  appendRows_(newSheet, nDate + 1, rows, [0]);
  return rows.length + "日分をコピーしました";
}

// ============================================================
// 予算計画（B列：年月 C：計画 D：計画の累計 E：実績 F：実績の累計 G：差額 H：差額の累計 I：引落し確定額）
// ============================================================

function mergeBudget_(oldSs, newSs) {
  const read = (sheet) => {
    const map = {};
    sheet.getDataRange().getValues().forEach((r) => {
      const m = String(r[1] || "").match(/^(\d{4})年(\d{1,2})月$/);
      if (m) map[m[1] + "-" + ("0" + m[2]).slice(-2)] = { label: m[1] + "年" + Number(m[2]) + "月", values: r.slice(2, 9) };
    });
    return map;
  };
  const oldMap = read(oldSs.getSheetByName("予算計画"));
  const newSheet = newSs.getSheetByName("予算計画");
  const newMap = read(newSheet);
  if (!Object.keys(oldMap).length) return "月の行が見つからないため、飛ばしました";

  // 同じ月は、項目ごとに「この家計簿（GAS版）に値があればそれを、空欄なら新しい家計簿の値を」使う。
  // 累計（D・F・H）は、新しい家計簿が次の自動実行で計算し直す
  const isBlank = (x) => x === "" || x === null || x === undefined;
  const merged = Object.assign({}, newMap);
  Object.keys(oldMap).forEach((k) => {
    if (!merged[k]) { merged[k] = oldMap[k]; return; }
    const values = merged[k].values.slice();
    oldMap[k].values.forEach((v, i) => { if (!isBlank(v)) values[i] = v; });
    merged[k] = { label: oldMap[k].label, values: values };
  });
  const keys = Object.keys(merged).sort();
  const rows = keys.map((k) => {
    const v = merged[k].values.concat(["", "", "", "", "", "", ""]).slice(0, 7);
    const blank = (x) => (x === null || x === undefined ? "" : x);
    return [merged[k].label, blank(v[0]), "", blank(v[2]), "", blank(v[4]), "", blank(v[6])].slice(0, 8);
  });

  // 新しい家計簿の、データが始まる行（最初の「yyyy年m月」の行。無ければ見出しの下の4行目）
  const values = newSheet.getDataRange().getValues();
  let start = values.findIndex((r) => /^\d{4}年\d{1,2}月$/.test(String(r[1] || ""))) + 1;
  if (start <= 0) start = 4;
  const need = start + rows.length - 1;
  if (newSheet.getMaxRows() < need) newSheet.insertRowsAfter(newSheet.getMaxRows(), need - newSheet.getMaxRows());
  const lastData = Math.max(newSheet.getLastRow(), start);
  newSheet.getRange(start, 2, lastData - start + 1, 8).clearContent();
  newSheet.getRange(start, 2, rows.length, 8).setValues(rows);
  return rows.length + "か月分にしました（" + rows[0][0] + "〜" + rows[rows.length - 1][0] + "）";
}

// ============================================================
// カテゴリ予算・送信元リスト（見出しの下の表を、そのまま置き換える）
// ============================================================

function replaceTable_(oldSs, newSs, name) {
  const oldSheet = oldSs.getSheetByName(name), newSheet = newSs.getSheetByName(name);
  if (!oldSheet) return "この家計簿に「" + name + "」シートが無いため、飛ばしました";
  if (!newSheet) return "新しい家計簿に「" + name + "」シートがまだ無いため、飛ばしました（次の自動実行のあとに、もう一度実行してください）";

  const headerRow = (values) => {
    for (let r = 0; r < Math.min(values.length, 10); r++) if (values[r].filter((v) => String(v).trim()).length >= 2) return r;
    return -1;
  };
  const oldValues = oldSheet.getDataRange().getValues();
  const newValues = newSheet.getDataRange().getValues();
  const oh = headerRow(oldValues), nh = headerRow(newValues);
  if (oh < 0 || nh < 0) return "見出しが見つからないため、飛ばしました";

  // 見出しの文字が同じ列どうしを対応させる（無ければ、同じ位置の列）
  const newHeaders = newValues[nh].map((v) => String(v).trim());
  const oldHeaders = oldValues[oh].map((v) => String(v).trim());
  const colMap = newHeaders.map((h, c) => (h ? (oldHeaders.indexOf(h) !== -1 ? oldHeaders.indexOf(h) : c) : -1));
  const firstCol = colMap.findIndex((c) => c !== -1);
  const lastCol = colMap.length - 1 - colMap.slice().reverse().findIndex((c) => c !== -1);

  const rows = oldValues.slice(oh + 1)
    .filter((r) => r.some((v) => String(v).trim()))
    .map((r) => colMap.slice(firstCol, lastCol + 1).map((c) => (c === -1 ? "" : r[c])));

  const start = nh + 2;
  const lastData = Math.max(newSheet.getLastRow(), start);
  // 見出しより下は、この表だけのはずなので、右端の列（見本の行の説明文など）まで消してから書く
  const clearCols = Math.max(lastCol + 1, newSheet.getLastColumn()) - firstCol;
  newSheet.getRange(start, firstCol + 1, lastData - start + 1, clearCols).clearContent();
  if (rows.length) {
    const need = start + rows.length - 1;
    if (newSheet.getMaxRows() < need) newSheet.insertRowsAfter(newSheet.getMaxRows(), need - newSheet.getMaxRows());
    newSheet.getRange(start, firstCol + 1, rows.length, lastCol - firstCol + 1).setValues(rows);
  }
  return rows.length + "行にしました";
}

// ============================================================
// この家計簿（GAS版）の自動実行を止める
// ============================================================

function stopTriggers_() {
  const triggers = ScriptApp.getProjectTriggers();
  triggers.forEach((t) => ScriptApp.deleteTrigger(t));
  return triggers.length ? triggers.length + "個のトリガーを止めました（同じ通知が2回届かなくなります）" : "動いているトリガーはありませんでした";
}
