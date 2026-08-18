/*
  A modal must not open carrying the previous row's errors.

  One modal element is reused for every row in a table. Open it for teacher A,
  get a PAN error back, close it, open it for teacher B — and A's error was
  still sitting there against B's name. Clearing was done at each call site,
  and the edit path did not do it.

  Run:  npm install --no-save jsdom jquery && node jstests/modal_errors.test.js
*/
const { JSDOM } = require("jsdom");
const fs = require("fs");
const nodePath = require("path");
const ROOT = nodePath.resolve(__dirname, "..");

const dom = new JSDOM(`<!doctype html><html><body>
  <div class="modal fade" id="m">
    <form id="f">
      <div class="mb-3">
        <input name="pan_number">
      </div>
      <div class="invalid-feedback d-block" id="err"></div>
    </form>
  </div>
</body></html>`, { url: "http://localhost/", runScripts: "dangerously" });
const { window } = dom;

const jqEl = window.document.createElement("script");
jqEl.textContent = fs.readFileSync(
  nodePath.join(nodePath.dirname(require.resolve("jquery")), "jquery.js"), "utf8");
window.document.head.appendChild(jqEl);
window.jQuery.ajaxSetup = function () {};

/*
  A stand-in for bootstrap.Modal that fires the same events the real one does.
  `show.bs.modal` is the whole point of this test, so a stub that stayed silent
  would prove nothing.
*/
window.bootstrap = { Modal: function (el) {
  const $el = window.jQuery(el);
  this.show = function () { $el.trigger("show.bs.modal").addClass("show"); };
  this.hide = function () { $el.trigger("hidden.bs.modal").removeClass("show"); };
} };

const appEl = window.document.createElement("script");
appEl.textContent = fs.readFileSync(ROOT + "/static/js/app.js", "utf8");
window.document.body.appendChild(appEl);

const $ = window.jQuery;
const GA = window.GA;
let failures = 0;
function check(label, condition) {
  console.log((condition ? "  ok   " : "  FAIL ") + label);
  if (!condition) failures++;
}

const modal = new window.bootstrap.Modal(window.document.getElementById("m"));
const $form = $("#f");

console.log("an error is shown when the server refuses");
GA.showErrors($form, { pan_number: "Asha Rao already holds this PAN." });
check("the message is in the form", $form.text().includes("already holds this PAN"));
check("the input is marked invalid", $('[name="pan_number"]').hasClass("is-invalid"));

console.log("re-opening the modal for a different row");
modal.hide();
modal.show();
check("the message is gone", !$form.text().includes("already holds this PAN"));
check("the input is no longer marked invalid",
      !$('[name="pan_number"]').hasClass("is-invalid"));

console.log("the ad-hoc message box is cleared too");
$("#err").text("The PAN verification service could not be reached.");
modal.hide();
modal.show();
check("#err is empty", $("#err").text() === "");

console.log("a fresh error still shows after the modal is reopened");
GA.showErrors($form, { pan_number: "That PAN could not be verified." });
check("the new message is shown", $form.text().includes("could not be verified"));

console.log("submitting again clears the old error first");
/* GA.submit() calls clearErrors before it posts, so a second attempt never
   shows the first attempt's message alongside the new one. */
GA.clearErrors($form);
check("cleared", !$form.text().includes("could not be verified"));

console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
