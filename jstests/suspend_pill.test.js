/*
  Runs the real static/js/app.js in a DOM and clicks things.

  Why: the suspend-info handler was written after the module's `return {…}`
  statement — unreachable code. The file parsed, `node --check` passed, the
  markup was in the page, and the click did nothing. Only executing it finds
  that.
*/
const { JSDOM } = require("jsdom");
const fs = require("fs");
const ROOT = require("path").resolve(__dirname, "..");
const path = ROOT + "/static/js/app.js";

const dom = new JSDOM(
  `<!doctype html><html><body><div id="tbl"></div></body></html>`,
  { url: "http://localhost/", runScripts: "dangerously" });
const { window } = dom;

// jQuery built against this window, installed as the globals app.js expects.
const jqSrc = fs.readFileSync(
  // `require.resolve("jquery/dist/…")` is blocked by the package's `exports`
  // map, so the file is located from the package root instead.
  require("path").join(require("path").dirname(require.resolve("jquery")),
                       "jquery.js"), "utf8");
const jqEl = window.document.createElement("script");
jqEl.textContent = jqSrc;
window.document.head.appendChild(jqEl);

// jsdom has no ajax transport and app.js configures one at load.
window.jQuery.ajaxSetup = function () {};
window.GA_IS_UNIVERSITY = true;

let shown = 0;
window.bootstrap = { Modal: function (el) { this.el = el;
  this.show = function () { shown++; }; this.hide = function () {}; } };

const el = window.document.createElement("script");
el.textContent = fs.readFileSync(path, "utf8");
window.document.body.appendChild(el);

const $ = window.jQuery;
const GA = window.GA;
let failures = 0;
function check(label, condition) {
  console.log((condition ? "  ok   " : "  FAIL ") + label);
  if (!condition) failures++;
}

const row = {
  id: "1", full_name: "Asha Rao", email: "t@a.edu",
  status: "SUSPENDED", suspended: true,
  suspension_reason: "Repeated unexplained absence since March.",
  suspended_at: "13 Aug 2026, 14:32", suspended_by: "ENGGU"
};

console.log("statusCell markup");
const html = GA.statusCell(row);
check("renders a button, not a plain span", /^<button/.test(html));
check("carries the reason", html.includes("Repeated unexplained absence"));
check("carries the date", html.includes("13 Aug 2026, 14:32"));

console.log("clicking it");
$("#tbl").html(html);
check("button is in the DOM", $("#tbl .ga-suspend-info").length === 1);
$("#tbl .ga-suspend-info").trigger("click");
check("a dialog was constructed", shown === 1);
const $dialog = $("body .modal");
check("the dialog is in the page", $dialog.length === 1);
const text = $dialog.text();
check("it shows the reason", text.includes("Repeated unexplained absence"));
check("it shows the date", text.includes("13 Aug 2026, 14:32"));
check("it names the university", text.includes("ENGGU"));
check("it names the teacher", text.includes("Asha Rao"));

console.log("after a table re-render (delegated, not bound)");
$("body .modal").remove();
shown = 0;
$("#tbl").empty().html(GA.statusCell(row));
$("#tbl .ga-suspend-info").trigger("click");
check("still opens", shown === 1);

console.log("an unsuspended row is untouched");
const plain = GA.statusCell({ status: "ACTIVE", suspended: false });
check("renders the ordinary pill", plain.includes("Active") && !plain.includes("ga-suspend-info"));

console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
