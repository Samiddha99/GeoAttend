"""
Adding, removing and delinking the disciplines an institute teaches.

An `InstituteAffiliation` row is a claim about two parties: "this institute
teaches pharmacy, and *that* university awards the degrees". Only one of them
can make the second half of that claim, which is what all the rules below come
down to.

**What an institute may do on its own.** Add a discipline as *autonomous*. That
is a statement about itself only — "we also teach pharmacy, and nobody else
awards it" — so nobody else has to agree. It may not name a university, because
that would be signing someone else's name.

**What a university may do.** Add a discipline affiliated to itself, take one
back, or delink it. Delinking sets `university = NULL` rather than deleting the
row: the institute still teaches the subject, it simply teaches it
autonomously now, and dropping the row would silently claim it had stopped.
That distinction is why the column is nullable in the first place.

**The boundary a university does not cross.** It may not touch a discipline
affiliated to a *different* university. An institute with engineering under
AKTU and pharmacy under a health-sciences university has two affiliating
bodies, and letting either rewrite the other's record would make the whole
per-discipline design pointless. The requirement says a university may change
"any discipline of an institute"; this reads that as any discipline that is
its own business — its own affiliations, and unclaimed ones — and refuses the
rest with a message naming who to ask.
"""
from django.db import transaction

from .models import ActivityLog, Discipline, InstituteAffiliation


class AffiliationError(Exception):
    """A refusal with a message meant to be shown to the person."""


def rows_for(institute):
    """
    Every discipline this institute teaches, with who affiliates it.

    Ordered by `Discipline.choices` rather than by the stored code, so the list
    reads the same here as on every form that offers it.
    """
    if institute is None:
        return []
    order = {value: i for i, (value, _) in enumerate(Discipline.choices)}
    labels = dict(Discipline.choices)
    rows = [{
        "id": str(a.id),
        "discipline": a.discipline,
        "discipline_label": labels.get(a.discipline, a.discipline),
        "university": (a.university.short_name or a.university.name
                       if a.university_id else ""),
        "university_id": str(a.university_id) if a.university_id else "",
        "autonomous": a.university_id is None,
    } for a in institute.affiliations.select_related("university")]
    rows.sort(key=lambda r: order.get(r["discipline"], len(order)))
    return rows


def available_disciplines(institute):
    """The ones not already on file — what an "add" control should offer."""
    taken = set(institute.affiliations.values_list("discipline", flat=True))
    return [{"value": value, "label": label}
            for value, label in Discipline.choices if value not in taken]


def _validate(codes):
    """Unknown codes are refused rather than stored and later mystifying."""
    codes = [c for c in (codes or []) if c]
    unknown = [c for c in codes if c not in Discipline.values]
    if unknown:
        raise AffiliationError(f"Unknown discipline: {', '.join(unknown)}.")
    if not codes:
        raise AffiliationError("Choose at least one discipline.")
    return codes


# --------------------------------------------------------------------------- #
#  What an institute may do for itself
# --------------------------------------------------------------------------- #
@transaction.atomic
def add_autonomous(*, institute, disciplines, actor):
    """
    Record disciplines this institute teaches under its own authority.

    Open to any institute, affiliated or not — an institute affiliated for
    engineering may still open an autonomous pharmacy wing, and needs nobody's
    permission to say so. Naming a university is what it cannot do alone.

    Disciplines already on file are left exactly as they are and reported.
    Overwriting one would let an affiliated institute quietly drop its
    university by re-adding the same discipline as autonomous, which is the
    whole rule inverted.
    """
    codes = _validate(disciplines)
    added, existing = [], []
    labels = dict(Discipline.choices)
    for code in codes:
        _, created = InstituteAffiliation.objects.get_or_create(
            institute=institute, discipline=code,
            defaults={"university": None})
        (added if created else existing).append(labels.get(code, code))
    # The stored flag has to follow the affiliation it depends on, or it is
    # right until the next time somebody changes one — see
    # academics.curriculum.sync_revoked.
    from academics.curriculum import sync_revoked

    sync_revoked(institute)
    if added:
        ActivityLog.log(actor=actor, action="DISCIPLINE_ADDED",
                        detail=f"{institute.name}: {', '.join(added)} (autonomous)")
    return {"added": added, "existing": existing}


# --------------------------------------------------------------------------- #
#  What a university may do
# --------------------------------------------------------------------------- #
def _may_touch(university, affiliation):
    """
    Is this row this university's business?

    Its own affiliations, and unclaimed (autonomous) ones. Never another
    university's — see the module note.
    """
    return (affiliation.university_id is None
            or affiliation.university_id == university.pk)


def _guard(university, affiliation):
    if not _may_touch(university, affiliation):
        owner = affiliation.university.short_name or affiliation.university.name
        raise AffiliationError(
            f"{affiliation.get_discipline_display()} is affiliated to {owner}, "
            "not to you. Only they can change it.")


@transaction.atomic
def set_affiliation(*, institute, disciplines, university, actor):
    """
    Affiliate these disciplines to this university, adding them if new.

    Used for both "add a discipline under us" and "take over one that was
    autonomous", because from the institute's side they are the same event:
    a university's name appears where none was.
    """
    codes = _validate(disciplines)
    labels = dict(Discipline.choices)
    changed, unchanged = [], []
    for code in codes:
        affiliation, created = InstituteAffiliation.objects.get_or_create(
            institute=institute, discipline=code,
            defaults={"university": university})
        if created:
            changed.append(labels.get(code, code))
            continue
        _guard(university, affiliation)
        if affiliation.university_id == university.pk:
            unchanged.append(labels.get(code, code))
            continue
        affiliation.university = university
        affiliation.save(update_fields=["university"])
        changed.append(labels.get(code, code))
    # The stored flag has to follow the affiliation it depends on, or it is
    # right until the next time somebody changes one — see
    # academics.curriculum.sync_revoked.
    from academics.curriculum import sync_revoked

    sync_revoked(institute)
    if changed:
        ActivityLog.log(actor=actor, action="DISCIPLINE_AFFILIATED",
                        detail=f"{institute.name}: {', '.join(changed)} -> {university}")
    return {"changed": changed, "unchanged": unchanged}


@transaction.atomic
def delink(*, institute, disciplines, university, actor):
    """
    Hand these disciplines back: the institute becomes autonomous for them.

    The row stays, with `university` cleared. The institute has not stopped
    teaching the subject — it has stopped answering to anyone for it — and
    deleting the row would say the first thing while meaning the second.

    The shared curriculum pushed under this affiliation stays where it is —
    those subjects have attendance against them, and withdrawing a syllabus is
    a separate decision from withdrawing an affiliation. What does change is
    that they stop being read-only: an institute nobody affiliates answers to
    nobody for its subjects either. `academics.curriculum.is_read_only` checks
    the affiliation and not just the stamp on the row, which is what makes
    that true rather than merely intended.
    """
    codes = _validate(disciplines)
    labels = dict(Discipline.choices)
    delinked, already = [], []
    for affiliation in institute.affiliations.filter(discipline__in=codes):
        _guard(university, affiliation)
        if affiliation.university_id is None:
            already.append(labels.get(affiliation.discipline, affiliation.discipline))
            continue
        affiliation.university = None
        affiliation.save(update_fields=["university"])
        delinked.append(labels.get(affiliation.discipline, affiliation.discipline))
    # The stored flag has to follow the affiliation it depends on, or it is
    # right until the next time somebody changes one — see
    # academics.curriculum.sync_revoked.
    from academics.curriculum import sync_revoked

    sync_revoked(institute)
    # Hand the adopted rows back. The link is what makes them read-only, so
    # leaving it would freeze the college's syllabus to a university that no
    # longer awards its degrees — see academics.catalogue.release.
    from academics.catalogue import release

    for code in codes:
        release(institute, code)
    if delinked:
        ActivityLog.log(actor=actor, action="DISCIPLINE_DELINKED",
                        detail=f"{institute.name}: {', '.join(delinked)} -> autonomous")
    return {"delinked": delinked, "already": already}


def contents_of(institute, discipline):
    """
    What sits inside a discipline, counted.

    Shown before anything is removed so the choice between "archive it all" and
    "leave it" is made against real numbers rather than a guess. Counts live
    rows only — an already-archived batch is not something the person is about
    to lose.
    """
    from academics.models import Batch, Department, StudentProfile, Subject
    from .models import User

    departments = Department.objects.filter(
        institute=institute, discipline=discipline)
    # Materialised, not passed as a queryset — see the note in
    # `archive_discipline_contents`. Kept the same here so the two agree about
    # what "in this discipline" means.
    ids = list(departments.values_list("id", flat=True))
    return {
        "departments": departments.filter(is_active=True).count(),
        "subjects": Subject.objects.filter(
            department_id__in=ids, is_active=True).count(),
        "batches": Batch.objects.filter(
            department_id__in=ids, is_active=True).count(),
        "students": StudentProfile.objects.filter(
            department_id__in=ids, is_active=True).count(),
        "teachers": User.objects.filter(
            department_id__in=ids, role=User.Role.TEACHER,
            is_active=True).count(),
    }


@transaction.atomic
def archive_discipline_contents(*, institute, discipline, actor):
    """
    Deactivate everything in this discipline. Nothing is deleted.

    Archiving rather than deleting is the established shape of this app —
    a batch archives, an in-use subject deactivates — and it is the right one
    here for a reason worth stating: deleting the students would take their
    attendance, feedback and absence history with them, and an institute
    tidying up a discipline it no longer offers is not asking to destroy three
    years of records. Archived rows vanish from every screen exactly as deleted
    ones would; re-adding the discipline brings them all back.

    Batches are handled through their `is_active` flag, which the selectors
    already treat as "hide this cohort and everything hanging off it", so the
    students disappear from attendance and reports without being touched
    individually — but they are marked inactive too, so the staff lists agree.
    """
    from academics.models import Batch, Department, StudentProfile, Subject
    from .models import User

    departments = Department.objects.filter(
        institute=institute, discipline=discipline)
    counts = contents_of(institute, discipline)

    # **The ids are fetched, not passed as a queryset.** `department__in=<qs>`
    # is a correlated subquery, and `django_mongodb_backend` cannot express one
    # inside an `update()`: Atlas rejects the generated pipeline with
    # "$in requires an array as a second argument, found: missing".
    #
    # sqlite runs it happily, which is exactly why the tests for this passed and
    # the first real removal did not. One extra round trip buys a query the
    # backend can actually run — do not "simplify" this back.
    ids = list(departments.values_list("id", flat=True))

    # **Only rows that are live right now are marked.** That marker is what
    # lets a later restore put each row back the way it was instead of turning
    # everything on: a cohort archived in 2023 and a cohort hidden by this
    # removal are indistinguishable afterwards without it. Filtering on
    # `is_active=True` before the update is the whole mechanism — a row already
    # archived is left unmarked, so restoring leaves it archived.
    archived = {"is_active": False, "status": "ARCHIVED",
                "archived_with_discipline": True}
    Subject.objects.filter(department_id__in=ids, is_active=True).update(**archived)
    Batch.objects.filter(department_id__in=ids, is_active=True).update(**archived)
    StudentProfile.objects.filter(department_id__in=ids, is_active=True).update(**archived)
    User.objects.filter(department_id__in=ids, role=User.Role.TEACHER,
                        is_active=True).update(**archived)
    Department.objects.filter(id__in=ids, is_active=True).update(**archived)

    ActivityLog.log(actor=actor, action="DISCIPLINE_CONTENTS_ARCHIVED",
                    detail=f"{institute.name}: {discipline} — "
                           f"{counts['departments']} dept, "
                           f"{counts['students']} students")
    return counts


@transaction.atomic
def remove_own(*, institute, discipline, archive, actor):
    """
    Unlist a discipline the institute holds autonomously.

    **Autonomous only.** An affiliated discipline is a record about the
    university as much as the institute, and letting a college walk away from
    its affiliating body through a settings page is not a decision this screen
    should be able to make. The university delinks first; then this is
    available.

    `archive` chooses what happens to the departments, subjects, batches,
    students and teachers inside it:

    * True — everything is deactivated, and disappears from every screen.
    * False — everything stays exactly as it is. Its departments simply stop
      being tied to an affiliation, which is the same state as every department
      that predates the discipline column: governed by nobody, editable by the
      institute. Nothing breaks, and re-adding the discipline re-ties them.

    The row is deleted either way, which is what puts the discipline back in
    the "Add a discipline" list.
    """
    affiliation = institute.affiliations.filter(discipline=discipline).first()
    if affiliation is None:
        raise AffiliationError("That discipline is not on file.")
    if affiliation.university_id is not None:
        owner = affiliation.university.short_name or affiliation.university.name
        raise AffiliationError(
            f"{affiliation.get_discipline_display()} is awarded by {owner}, so "
            "it is not yours to remove. Ask them to delink it first — then you "
            "can remove it here.")
    if institute.affiliations.count() == 1:
        raise AffiliationError(
            "This is the only discipline on file. An institute teaching "
            "nothing is not a state any screen can show, so add another one "
            "first.")

    label = affiliation.get_discipline_display()
    counts = (archive_discipline_contents(
        institute=institute, discipline=discipline, actor=actor)
        if archive else contents_of(institute, discipline))
    from academics.catalogue import release

    release(institute, discipline)
    affiliation.delete()
    from academics.curriculum import sync_revoked

    sync_revoked(institute)
    ActivityLog.log(
        actor=actor, action="DISCIPLINE_REMOVED_BY_INSTITUTE",
        detail=f"{institute.name}: {label} "
               f"({'contents archived' if archive else 'contents kept'})")
    return {"discipline": discipline, "label": label,
            "archived": archive, "counts": counts}


@transaction.atomic
def remove(*, institute, disciplines, university, actor):
    """
    Drop these disciplines from the institute's record entirely.

    Stronger than delinking, and the right verb only when the institute does
    not teach the subject at all. Refused when it would leave the institute
    with nothing: an institute teaching no disciplines is not a state any
    screen in this application knows how to render, and the person almost
    certainly meant to delink.
    """
    codes = _validate(disciplines)
    labels = dict(Discipline.choices)
    rows = list(institute.affiliations.filter(discipline__in=codes))
    for affiliation in rows:
        _guard(university, affiliation)
    if rows and institute.affiliations.count() == len(rows):
        raise AffiliationError(
            "That would leave the institute teaching nothing. Delink instead "
            "if it still runs the course without you.")
    removed = [labels.get(a.discipline, a.discipline) for a in rows]
    institute.affiliations.filter(
        pk__in=[a.pk for a in rows]).delete()
    # The stored flag has to follow the affiliation it depends on, or it is
    # right until the next time somebody changes one — see
    # academics.curriculum.sync_revoked.
    from academics.curriculum import sync_revoked

    sync_revoked(institute)
    if removed:
        ActivityLog.log(actor=actor, action="DISCIPLINE_REMOVED",
                        detail=f"{institute.name}: {', '.join(removed)}")
    return {"removed": removed}
