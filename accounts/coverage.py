"""
The disciplines a university covers — adding one, and withdrawing one.

The twin of `accounts/affiliations.py`, which does the same job from the
institute's side. Kept apart because the two answer different questions:
`InstituteAffiliation` says "this college teaches pharmacy and *that*
university awards it", while `UniversityDiscipline` says only "we award
pharmacy". One is a claim about two parties; the other is a claim about one.

**Adding is unremarkable.** A university saying it awards a subject needs
nobody's agreement, so there is no invitation, no confirmation and nothing to
propagate. It simply becomes available in the institute signup dropdown and in
the catalogue's discipline list.

**Withdrawing is not.** Three things sit inside a covered discipline, and they
are not equally the university's to dispose of:

  1. Its own catalogue — departments, batches and subjects it publishes. These
     are entirely its own, and the person is offered the same choice the
     institute screen offers: archive them, or leave them exactly as they are.

  2. Institutes affiliated to it for that discipline. These are **delinked, not
     asked about.** An affiliation is a claim that this university awards the
     degree; once it does not, leaving the row would be a claim nobody can
     back. Delinking sets `university = NULL`, so each college keeps teaching
     the subject autonomously — which is true, and is the only outcome that
     does not silently rewrite what a college does.

  3. Those colleges' departments, students, batches and attendance. **Untouched,
     and not on offer here.** A university withdrawing from a discipline is not
     closing anybody's department, and a screen that could do so by accident is
     a screen that eventually will. `academics.catalogue.release` hands the
     adopted rows back to the college as its own, so the wing carries on under
     new ownership rather than going dark.

Nothing is deleted anywhere. Re-adding the discipline restores the university's
own catalogue; it does not re-affiliate the colleges, because that half needs
their agreement again.
"""
from django.db import transaction

from .models import ActivityLog, Discipline, UniversityDiscipline


class CoverageError(Exception):
    """A refusal with a message meant to be shown to the person."""


def rows_for(university):
    """
    Every discipline this university awards, with what hangs off each one.

    Ordered by `Discipline.choices` rather than by the stored code, so the list
    reads the same here as on every form that offers it.
    """
    if university is None:
        return []
    from academics.catalogue import UniversityDepartment
    from django.db.models import Count

    order = {value: i for i, (value, _) in enumerate(Discipline.choices)}
    labels = dict(Discipline.choices)

    # Two grouped queries rather than filtered annotations on one. Several
    # filtered `Count`s over different relations fan out on MongoDB — one
    # lookup-and-unwind each, multiplying the rows they are counting. That bug
    # inflated the department screen's counts twice before it was understood.
    published = dict(
        UniversityDepartment.objects.filter(university=university)
        .values_list("discipline").annotate(n=Count("id"))
        .values_list("discipline", "n"))
    from .models import InstituteAffiliation

    affiliated = dict(
        InstituteAffiliation.objects.filter(university=university)
        .values_list("discipline").annotate(n=Count("id"))
        .values_list("discipline", "n"))

    rows = [{
        "id": str(d.id),
        "discipline": d.discipline,
        "discipline_label": labels.get(d.discipline, d.discipline),
        "departments": published.get(d.discipline, 0),
        "institutes": affiliated.get(d.discipline, 0),
    } for d in university.disciplines.all()]
    rows.sort(key=lambda r: order.get(r["discipline"], len(order)))
    return rows


def available(university):
    """The ones not already covered — what an "add" control should offer."""
    if university is None:
        return []
    taken = set(university.disciplines.values_list("discipline", flat=True))
    return [{"value": value, "label": label}
            for value, label in Discipline.choices if value not in taken]


def _validate(codes):
    """Unknown codes are refused rather than stored and later mystifying."""
    codes = [c for c in (codes or []) if c]
    unknown = [c for c in codes if c not in Discipline.values]
    if unknown:
        raise CoverageError(f"Unknown discipline: {', '.join(unknown)}.")
    if not codes:
        raise CoverageError("Choose at least one discipline.")
    return codes


@transaction.atomic
def add(*, university, disciplines, actor=None):
    """
    Record disciplines this university awards.

    Idempotent by discipline: one already on file is reported back rather than
    raised on, because a person ticking four boxes of which one was already
    there meant to end up with all four, not with an error.
    """
    codes = _validate(disciplines)
    labels = dict(Discipline.choices)
    held = set(university.disciplines.values_list("discipline", flat=True))

    added, existing = [], []
    for code in codes:
        if code in held:
            existing.append(labels[code])
            continue
        UniversityDiscipline.objects.create(university=university,
                                            discipline=code)
        added.append(labels[code])

    if added:
        ActivityLog.log(actor=actor, action="UNIVERSITY_DISCIPLINE_ADDED",
                        detail=f"{university.name}: {', '.join(added)}")
    return {"added": added, "existing": existing}


def contents_of(university, discipline):
    """
    What sits inside a covered discipline, counted.

    Shown before anything is withdrawn so the choice is made against real
    numbers rather than a guess. Counts live rows only — an already-archived
    subject is not something the person is about to lose.

    `institutes` is the count that matters most and the one the person is least
    likely to have in mind: it is the number of colleges whose affiliation is
    about to end.
    """
    from academics.catalogue import (
        UniversityBatch,
        UniversityDepartment,
        UniversitySubject,
    )
    from core.enums import RowStatus

    from .models import InstituteAffiliation

    departments = UniversityDepartment.objects.filter(
        university=university, discipline=discipline)
    # Materialised rather than passed as a queryset. `department__in=<qs>` is a
    # correlated subquery and `django_mongodb_backend` cannot express one — see
    # the long note in accounts/affiliations.archive_discipline_contents. sqlite
    # runs it happily, which is exactly how this shape reaches production.
    ids = list(departments.values_list("id", flat=True))
    return {
        "departments": departments.exclude(status=RowStatus.ARCHIVED).count(),
        "batches": UniversityBatch.objects.filter(department_id__in=ids)
                                          .exclude(status=RowStatus.ARCHIVED)
                                          .count(),
        "subjects": UniversitySubject.objects.filter(department_id__in=ids)
                                             .exclude(status=RowStatus.ARCHIVED)
                                             .count(),
        "institutes": InstituteAffiliation.objects.filter(
            university=university, discipline=discipline).count(),
    }


@transaction.atomic
def archive_catalogue(*, university, discipline, actor=None):
    """
    Withdraw this discipline's catalogue. Nothing is deleted.

    Only the university's own entries. The colleges' copies are left running —
    they are the college's data now, and archiving them from here would close a
    working department in a building this account has never seen.
    """
    from academics.catalogue import (
        UniversityBatch,
        UniversityDepartment,
        UniversitySubject,
    )
    from core.enums import RowStatus

    counts = contents_of(university, discipline)
    departments = UniversityDepartment.objects.filter(
        university=university, discipline=discipline)
    ids = list(departments.values_list("id", flat=True))

    UniversitySubject.objects.filter(department_id__in=ids).update(
        status=RowStatus.ARCHIVED)
    UniversityBatch.objects.filter(department_id__in=ids).update(
        status=RowStatus.ARCHIVED)
    UniversityDepartment.objects.filter(id__in=ids).update(
        status=RowStatus.ARCHIVED)

    ActivityLog.log(actor=actor, action="UNIVERSITY_CATALOGUE_ARCHIVED",
                    detail=f"{university.name}: {discipline} — "
                           f"{counts['departments']} dept, "
                           f"{counts['subjects']} subjects")
    return counts


@transaction.atomic
def remove(*, university, discipline, archive, actor=None):
    """
    Stop awarding a discipline.

    `archive` decides what happens to this university's **own catalogue**:
    True archives it, False leaves it exactly as it is. It does not decide
    anything about the colleges — see the module docstring for why that is not
    on offer.

    Every institute affiliated for this discipline is delinked and becomes
    autonomous in it. Their departments, students and attendance are untouched;
    `academics.catalogue.release` hands the adopted rows back to them so
    nothing they run stops working.
    """
    if discipline not in Discipline.values:
        raise CoverageError("Unknown discipline.")
    row = university.disciplines.filter(discipline=discipline).first()
    if row is None:
        raise CoverageError("That discipline is not on file.")
    if university.disciplines.count() == 1:
        raise CoverageError(
            "This is the only discipline on file. A university awarding "
            "nothing is not a state any screen can show, so add another one "
            "first.")

    label = row.get_discipline_display()
    counts = contents_of(university, discipline)
    if archive:
        archive_catalogue(university=university, discipline=discipline,
                          actor=actor)

    # Delinked, not asked about. An affiliation to a university that no longer
    # awards the degree is a claim nobody can back.
    from .affiliations import delink
    from .models import InstituteAffiliation

    institutes = {a.institute for a in InstituteAffiliation.objects.filter(
        university=university, discipline=discipline)
        .select_related("institute")}
    for institute in institutes:
        delink(institute=institute, disciplines=[discipline],
               university=university, actor=actor)

    row.delete()
    ActivityLog.log(
        actor=actor, action="UNIVERSITY_DISCIPLINE_REMOVED",
        detail=f"{university.name}: {label} "
               f"({'catalogue archived' if archive else 'catalogue kept'}, "
               f"{len(institutes)} institute(s) delinked)")
    return {"discipline": discipline, "label": label, "archived": archive,
            "counts": counts, "delinked": len(institutes)}
