# Browser-side checks

`node --check` proves a file parses. It does not prove the code runs — the
suspend-pill handler was written after the module's `return {…}` statement,
which is unreachable, and every other check in the project stayed green: the
file parsed, the template rendered, the markup was in the page, and the click
did nothing.

These run the real `static/js/app.js` in a DOM and click things.

```
npm install --no-save jsdom jquery
node jstests/suspend_pill.test.js
node jstests/modal_errors.test.js
```

* `suspend_pill` — the status pill opens a dialog with the reason and date.
* `modal_errors` — a modal never opens carrying the previous row's errors.
  One modal element is reused for every row in a table, so an error raised
  against one teacher used to greet whoever was opened next.

Exit code 0 if everything passed. Each check prints `ok` or `FAIL`, so a
failure names itself rather than pointing at a line number.
