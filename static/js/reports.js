/* Shared filter-bar behaviour for every analytics screen. */
(function ($, GA) {
  "use strict";

  GA.filters = function () {
    return GA.query({
      start: $("#f-start").val(),
      end: $("#f-end").val(),
      department: $("#f-department").val(),
      batch: $("#f-batch").val(),
      subject: $("#f-subject").val(),
      semester: $("#f-semester").val(),
      teacher: $("#f-teacher").val()
    });
  };

  function iso(d) { return d.toISOString().slice(0, 10); }

  GA.initFilters = function (onApply) {
    // onApply() returns $.when(...) over every request the screen fires, so
    // the button keeps spinning until the last one lands rather than the first.
    $("#filter-form").on("submit", function (e) {
      e.preventDefault();
      GA.spin($(this).find('button[type="submit"]'), onApply());
    });
    $("#f-reset").on("click", function () {
      $("#filter-form")[0].reset();
      $("#f-department, #f-batch, #f-subject, #f-semester, #f-teacher").val("");
      $("#f-subject option").show();          // undo any narrowing
      $(".ga-chip.quick").removeClass("sel").filter('[data-range="year"]').addClass("sel");
      GA.spin(this, onApply());
    });

    // Department and semester both narrow the subject list; applying them
    // together is what makes "semester 3 of CSE" a usable pair of filters.
    function narrowSubjects() {
      const d = $("#f-department").val(), sem = $("#f-semester").val();
      $("#f-batch option").each(function () {
        const own = $(this).data("dept");
        $(this).toggle(!d || !own || String(own) === String(d));
      });
      let hidCurrent = false;
      $("#f-subject option").each(function () {
        if (!$(this).val()) return;                      // keep "All subjects"
        const own = $(this).data("dept"), mine = $(this).data("sem");
        const show = (!d || !own || String(own) === String(d)) &&
                     (!sem || String(mine) === String(sem));
        $(this).toggle(show);
        if (!show && $(this).is(":selected")) hidCurrent = true;
      });
      // Only clear the subject if the narrowing actually hid the chosen one.
      if (hidCurrent) $("#f-subject").val("");
    }

    $("#f-department").on("change", function () {
      $("#f-batch").val("");
      narrowSubjects();
      onApply();
    });
    $("#f-semester").on("change", function () {
      narrowSubjects();
      onApply();
    });
    $("#f-batch, #f-subject, #f-teacher").on("change", onApply);

    $(".ga-chip.quick").on("click", function () {
      $(".ga-chip.quick").removeClass("sel");
      $(this).addClass("sel");
      const r = $(this).data("range");
      const today = new Date();
      let start = new Date(today.getFullYear(), 0, 1);
      if (r === "today") start = today;
      else if (r === "7") start = new Date(today.getTime() - 6 * 864e5);
      else if (r === "30") start = new Date(today.getTime() - 29 * 864e5);
      else if (r === "month") start = new Date(today.getFullYear(), today.getMonth(), 1);
      $("#f-start").val(iso(start));
      $("#f-end").val(iso(today));
      onApply();
    });
  };
})(jQuery, window.GA);
