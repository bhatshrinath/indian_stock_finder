/**
 * Starts the Stock Finder workflows automatically, so nobody has to press anything.
 *
 *   08:40 every weekday  -> "Live prices"  (it then runs the whole day by itself and starts the evening scan)
 *   16:40 every weekday  -> "Daily scan"   (backup only; skipped when today's scan already ran)
 *
 * One-time setup (about 10 minutes) is described in the README under "Automatic start".
 * Why this exists: GitHub's free scheduler does not fire for this repository, but Google's does.
 */
const OWNER = "bhatshrinath";
const REPO = "indian_stock_finder";

function startLivePrices() {
  start_("live-session.yml");
}

function startDailyScan() {
  if (scanAlreadyDone_()) { console.log("Today's scan already ran: nothing to do"); return; }
  start_("daily.yml");
}

function start_(workflow) {
  const weekday = Number(Utilities.formatDate(new Date(), "Asia/Kolkata", "u"));   // 1 = Monday ... 7 = Sunday
  if (weekday > 5) { console.log("Weekend: nothing to do"); return; }
  const token = PropertiesService.getScriptProperties().getProperty("GITHUB_TOKEN");
  if (!token) { throw new Error("Add the GITHUB_TOKEN script property first (see the README)."); }
  const res = UrlFetchApp.fetch(
    "https://api.github.com/repos/" + OWNER + "/" + REPO + "/actions/workflows/" + workflow + "/dispatches", {
      method: "post",
      contentType: "application/json",
      headers: { Authorization: "Bearer " + token, Accept: "application/vnd.github+json" },
      payload: JSON.stringify({ ref: "main" }),
      muteHttpExceptions: true,
    });
  console.log(workflow + " -> HTTP " + res.getResponseCode() + " (204 means started)");
}

function scanAlreadyDone_() {
  try {
    const url = "https://" + OWNER + ".github.io/" + REPO + "/data/latest.json?t=" + Date.now();
    const generated = JSON.parse(UrlFetchApp.fetch(url, { muteHttpExceptions: true }).getContentText()).generated;
    return Date.now() - new Date(generated).getTime() < 90 * 60 * 1000;
  } catch (e) {
    return false;
  }
}

/** Run this ONCE (select "createTriggers" and press Run). */
function createTriggers() {
  ScriptApp.getProjectTriggers().forEach(function (t) { ScriptApp.deleteTrigger(t); });
  ScriptApp.newTrigger("startLivePrices").timeBased().everyDays(1).atHour(8).nearMinute(40).inTimezone("Asia/Kolkata").create();
  ScriptApp.newTrigger("startDailyScan").timeBased().everyDays(1).atHour(16).nearMinute(40).inTimezone("Asia/Kolkata").create();
}
