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
      subject_type: $("#f-subject-type").val(),
      degree: $("#f-degree").val(),
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
      $("#f-department, #f-batch, #f-subject, #f-subject-type, #f-degree, #f-semester, #f-teacher").val("");
      $("#f-subject option, #f-subject optgroup").show();   // undo any narrowing
      $(".ga-chip.quick").removeClass("sel").filter('[data-range="year"]').addClass("sel");
      GA.spin(this, onApply());
    });

    // Department, semester, degree and type all narrow the subject list;
    // applying them together is what makes "the semester 3 CSE bachelor
    // practicals" a usable set of filters.
    function narrowSubjects() {
      const d = $("#f-department").val(), sem = $("#f-semester").val(),
            type = $("#f-subject-type").val(), deg = $("#f-degree").val();
      $("#f-batch option").each(function () {
        const own = $(this).data("dept");
        $(this).toggle(!d || !own || String(own) === String(d));
      });
      let hidCurrent = false;
      // Counted per group as we go. The options live inside <optgroup>s now,
      // and hiding every option in a group leaves the heading behind in most
      // browsers — so narrowing to Practical would still show an empty
      // "Theory" heading unless the group is hidden too.
      const kept = {};
      $("#f-subject option").each(function () {
        if (!$(this).val()) return;                      // keep "All subjects"
        const own = $(this).data("dept"), mine = $(this).data("sem"),
              kind = $(this).data("type"), deg2 = $(this).data("degree");
        const show = (!d || !own || String(own) === String(d)) &&
                     (!sem || String(mine) === String(sem)) &&
                     (!type || !kind || String(kind) === String(type)) &&
                     (!deg || !deg2 || String(deg2) === String(deg));
        $(this).toggle(show);
        if (!show && $(this).is(":selected")) hidCurrent = true;
        const group = $(this).closest("optgroup").attr("label");
        if (group) kept[group] = (kept[group] || 0) + (show ? 1 : 0);
      });
      $("#f-subject optgroup").each(function () {
        $(this).toggle(kept[$(this).attr("label")] > 0);
      });
      // Only clear the subject if the narrowing actually hid the chosen one.
      if (hidCurrent) $("#f-subject").val("");
    }

    $("#f-department").on("change", function () {
      $("#f-batch").val("");
      narrowSubjects();
      onApply();
    });
    $("#f-semester, #f-subject-type, #f-degree").on("change", function () {
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
