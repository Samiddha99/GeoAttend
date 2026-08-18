"""
The shared curriculum: subjects and batches a university defines once and every
institute it affiliates then runs.

**Why the rows are copied rather than shared.** A `Subject` hangs off a
`Department`, which hangs off an `Institute`. One row visible to twelve
institutes would mean making that chain optional, and every attendance query,
enrolment, allocation, report and export in the project joins through it. So a
university's subject is *materialised*: one ordinary `Subject` in each
affiliated institute's matching department, stamped with `owner_university` so
everything downstream keeps working unchanged and the institute can still be
told, truthfully, that it does not own this row.

The cost is duplication, and it is paid deliberately. The copies are kept
together by their natural key — the university, the department *code*, and the
subject code or batch label — so an edit or an archive finds its siblings
without a second table to keep in step.

**Departments are matched by code, not by name.** "Computer Science &
Engineering" and "Computer Science and Engineering" are the same department to
a registrar and different strings to a database; CSE is CSE. An institute with
no department of that code is skipped and reported, rather than having one
invented for it — a university deciding an institute now has a Pharmacy
department is a bigger claim than pushing a syllabus.

**Who is affected.** Only institutes this university *affiliates*. One it
merely invited runs its own curriculum, and so does an autonomous institute:
that is the distinction `accounts.scoping.affiliates` exists to draw.
"""
from django.db import IntegrityError, transaction

from core.enums import RowStatus

from .models import Batch, Department, Subject


def affiliated_institutes(university):
    """The institutes whose curriculum this university sets."""
    from accounts.models import Institute

    if university is None:
        return Institute.objects.none()
    return Institute.objects.filter(
        affiliations__university=university).distinct()


def target_departments(university, code):
    """
    One department per affiliated institute, matched on code.

    Institutes without that department simply do not appear — see the module
    note on why they are skipped rather than given one.
    """
    return Department.objects.filter(
        institute__in=affiliated_institutes(university),
        code__iexact=(code or "").strip(),
        is_active=True,
    ).select_related("institute")


def department_codes(university):
    """
    The codes a university can push to, with how many institutes have each.

    Offered instead of a department list because a university does not pick a
    department — it picks a code, and the push lands in every institute that
    has one.
    """
    rows = {}
    for department in Department.objects.filter(
            institute__in=affiliated_institutes(university), is_active=True
    ).select_related("institute").order_by("code"):
        entry = rows.setdefault(department.code.upper(), {
            "code": department.code.upper(),
            "name": department.name,
            "institutes": 0,
        })
        entry["institutes"] += 1
    return sorted(rows.values(), key=lambda r: r["code"])


# --------------------------------------------------------------------------- #
#  Ownership
# --------------------------------------------------------------------------- #
def governing_university(department):
    """
    The university that affiliates this department's discipline, or None.

    The whole per-discipline rule funnels through here. A department with no
    discipline on file returns None — see the note on the field itself: unset
    means "nobody affiliates this", which is how every department behaved
    before the column existed.
    """
    from accounts.models import InstituteAffiliation

    if department is None or not getattr(department, "discipline", ""):
        return None
    affiliation = InstituteAffiliation.objects.filter(
        institute_id=department.institute_id,
        discipline=department.discipline,
    ).select_related("university").first()
    return affiliation.university if affiliation else None


def cascade_revoked(department):
    """Push one department's flag down to its subjects, batches and people."""
    from accounts.models import User

    from .models import Batch, StudentProfile, Subject

    value = department.is_revoked
    for model in (Subject, Batch, StudentProfile):
        model.objects.filter(department=department).exclude(
            is_revoked=value).update(is_revoked=value)
    User.objects.filter(department=department, role=User.Role.TEACHER).exclude(
        is_revoked=value).update(is_revoked=value)


def sync_revoked(institute):
    """
    Recompute `is_revoked` for everything in one institute.

    Called from every place an affiliation can change — added, removed,
    delinked, or a department moved to a different discipline. One function so
    the flag cannot be right in three of those paths and stale in the fourth,
    which is the failure mode a stored flag invites and the reason the value
    was computed on the fly before.

    Cheap: five bulk updates, no per-row work, and it only runs on an action
    that already changes the institute's shape.

    Returns how many departments came out revoked, so a caller can say so.
    """
    from accounts.models import InstituteAffiliation
    from accounts.models import User

    from .models import Batch, Department, StudentProfile, Subject

    held = set(InstituteAffiliation.objects.filter(institute=institute)
               .values_list("discipline", flat=True))
    departments = list(Department.objects.filter(institute=institute))
    # A department with no discipline recorded is governed by nobody, which has
    # never meant revoked — every department predating the column is in that
    # state and must keep working.
    revoked = [d.pk for d in departments
               if d.discipline and d.discipline not in held]
    fine = [d.pk for d in departments if d.pk not in set(revoked)]

    for ids, value in ((revoked, True), (fine, False)):
        if not ids:
            continue
        Department.objects.filter(pk__in=ids).update(is_revoked=value)
        Subject.objects.filter(department_id__in=ids).update(is_revoked=value)
        Batch.objects.filter(department_id__in=ids).update(is_revoked=value)
        StudentProfile.objects.filter(department_id__in=ids).update(is_revoked=value)
        User.objects.filter(department_id__in=ids,
                            role=User.Role.TEACHER).update(is_revoked=value)
    return len(revoked)


def revoked_department_ids(departments):
    """
    Departments sitting in a discipline their institute no longer holds.

    That is what "revoked" means here, and it is a state with exactly one
    cause: somebody removed the discipline from the institute's record. The
    department is still there, its students are still there, and nothing about
    it is broken — it simply belongs to a wing the institute has stopped
    offering.

    Worth distinguishing from a merely inactive row, which is why the flag is
    computed rather than inferred from `is_active`. A batch archived on its own
    is a cohort that finished; a batch in a revoked discipline is part of a
    department that no longer answers to anything. The first is routine and the
    second is not, and a status column that showed both as "Archived" would
    hide the difference at exactly the moment it matters.

    Takes an iterable of departments and returns the ids, so a list endpoint
    can resolve the whole page in one query instead of one per row.
    """
    from accounts.models import InstituteAffiliation

    departments = [d for d in departments if getattr(d, "discipline", "")]
    if not departments:
        return set()
    held = {
        (a.institute_id, a.discipline)
        for a in InstituteAffiliation.objects.filter(
            institute_id__in={d.institute_id for d in departments}
        ).only("institute_id", "discipline")
    }
    return {d.pk for d in departments
            if (d.institute_id, d.discipline) not in held}


# Row state, in the order that decides which wins.
STATE_ACTIVE = "active"
STATE_ARCHIVED = "archived"
STATE_REVOKED = "revoked"


def department_states(departments):
    """
    `{department_id: state}` for a page's worth of departments.

    Reads the stored fields now. It used to derive both from the affiliation
    table on every request, which is where the counting bug came from: the
    derived value collapsed "revoked" and "archived" into one answer, so a
    revoked department's students could not be counted as active because they
    were no longer described as active anywhere.
    """
    return {
        d.pk: (STATE_REVOKED if d.is_revoked
               else STATE_ACTIVE if d.status == RowStatus.ACTIVE
               else STATE_ARCHIVED)
        for d in departments
    }


def effective_state(row, department_state=None):
    """
    What a row's status column should say.

    Revoked outranks everything, because it is the fact that explains the
    others. Below that the row speaks for itself — and *only* for itself:
    nothing here consults the department's status any more. A student in an
    archived department is still an active student, and pretending otherwise
    is what made the counts wrong.

    `department_state` is accepted and ignored, so the older call sites keep
    working while they are being converted.
    """
    if getattr(row, "is_revoked", False):
        return STATE_REVOKED
    status = getattr(row, "status", None)
    if status == RowStatus.ARCHIVED:
        return STATE_ARCHIVED
    if status == RowStatus.INVITED:
        return "invited"
    return STATE_ACTIVE


def live_departments(queryset):
    """
    Departments that are active *and* whose discipline is still on file.

    What every filter, dropdown and statistic should be built from. An archived
    or revoked department is something you look at on its own management
    screen, not something you file a new subject under or count in an average.
    """
    queryset = queryset.filter(is_active=True)
    dead = revoked_department_ids(queryset)
    return queryset.exclude(pk__in=dead) if dead else queryset


def is_revoked(department):
    """Is this one department's discipline off its institute's record?"""
    if department is None:
        return False
    return department.pk in revoked_department_ids([department])


def reactivate_department_contents(department, actor=None):
    """
    Put a department and its contents back to what they were before the
    discipline was removed.

    Restores **only** the rows that removal switched off — the ones carrying
    `archived_with_discipline`. Anything archived for its own reasons before
    that, a cohort that graduated or a subject deliberately retired, stays
    archived. The marker is set by
    `accounts.affiliations.archive_discipline_contents` and cleared here, so it
    never describes anything but the removal currently in force.

    An earlier version of this restored everything unconditionally, because
    nothing recorded which rows were which. That turned a graduated 2018 cohort
    back on alongside the wing being reopened. The field exists to stop that.

    A department archived before the marker existed has no snapshot, so nothing
    is turned on and the rows stay visibly archived for someone to restore
    deliberately. Safe direction: nothing is lost, and nothing reappears
    unasked.
    """
    from accounts.models import ActivityLog, User

    from .models import Batch, StudentProfile, Subject

    counts = {
        "subjects": Subject.objects.filter(
            department=department, archived_with_discipline=True).update(
            is_active=True, status=RowStatus.ACTIVE,
            archived_with_discipline=False),
        "batches": Batch.objects.filter(
            department=department, archived_with_discipline=True).update(
            is_active=True, status=RowStatus.ACTIVE,
            archived_with_discipline=False),
        "students": StudentProfile.objects.filter(
            department=department, archived_with_discipline=True).update(
            is_active=True, status=RowStatus.ACTIVE,
            archived_with_discipline=False),
        "teachers": User.objects.filter(
            department=department, role=User.Role.TEACHER,
            archived_with_discipline=True).update(
            is_active=True, status=RowStatus.ACTIVE,
            archived_with_discipline=False),
    }
    if department.archived_with_discipline:
        department.is_active = True
        department.status = RowStatus.ACTIVE
        department.archived_with_discipline = False
        department.save(update_fields=["is_active", "status",
                                       "archived_with_discipline"])
        counts["department_restored"] = True
    else:
        # It was already archived before the removal, or the person archived it
        # by hand afterwards. Either way its state is its own and not this
        # function's to overwrite.
        counts["department_restored"] = False
    if actor is not None:
        ActivityLog.log(
            actor=actor, action="DEPARTMENT_REACTIVATED",
            detail=f"{department.name} ({department.code}) — "
                   f"{counts['students']} students, {counts['batches']} batches")
    return counts


def selectable_disciplines(user, institute):
    """
    The disciplines this account may put a department in, for this institute.

    For an institute's head: the ones it holds **autonomously**. A department in
    an affiliated discipline is the university's to define — its subjects and
    batches already are — so offering the institute a discipline it does not
    govern would be offering it a department it could not then edit.

    For a university: the ones it affiliates here, and only those. It does not
    get to define departments in a discipline another university awards, for
    the same reason it cannot touch that affiliation at all.

    An institute with no autonomous discipline gets an empty list, which is the
    correct answer and not an error: it has no department of its own to create.
    The screen says so rather than showing an empty dropdown.
    """
    from accounts.models import Discipline, InstituteAffiliation

    if institute is None:
        return []
    rows = InstituteAffiliation.objects.filter(institute=institute)
    if getattr(user, "is_university", False):
        rows = rows.filter(university_id=user.university_id)
    else:
        rows = rows.filter(university__isnull=True)
    held = set(rows.values_list("discipline", flat=True))
    return [{"value": value, "label": label}
            for value, label in Discipline.choices if value in held]


def may_define_department(user, department):
    """
    May this account change a department's name, code or discipline?

    Adopted departments are the university's: the name and code came from the
    catalogue, and an institute editing them would make its copy disagree with
    every other college running the same syllabus. Autonomous and grandfathered
    ones are the institute's.

    `source` is the whole test — the link *is* the claim, so grandfathering
    needed no special case: a legacy department has no link and reads as the
    institute's without anything being written to say so.

    Not the same question as who may change its HoD. Running the department
    stays with the institute whatever the affiliation, because a university
    setting a syllabus has no view on which of the institute's staff heads the
    office. See `academics.views.api_department_save`.
    """
    if department is None:
        return True
    if getattr(department, "source_id", None) is None:
        return not getattr(user, "is_university", False)
    return bool(getattr(user, "is_university", False))


def is_read_only(row, user):
    """
    May this account edit this subject or batch?

    **The link is the rule.** A row adopted from the university's catalogue —
    `source` set — is the university's to change; anything else is the
    institute's. That covers all three cases in one test: an autonomous
    department's rows have no source, a grandfathered department's rows have no
    source, and an adopted department's rows do.

    This replaced a rule that keyed on the *department's* discipline being
    affiliated. That version locked rows the institute had created itself years
    earlier, which is why grandfathering needed inventing; keying on the link
    makes the grandfather case fall out for free rather than being a special
    case bolted on.
    """
    if getattr(row, "source_id", None) is None:
        return False
    return not getattr(user, "is_university", False)


def assert_writable(row, user):
    """Raise the message the API should show, or return quietly."""
    if is_read_only(row, user):
        raise PermissionError(
            "This is set by your affiliating university, so it cannot be "
            "changed here. Ask them to amend it.")


# --------------------------------------------------------------------------- #
#  Pushing
# --------------------------------------------------------------------------- #
@transaction.atomic
def push_subject(*, university, code, fields, department_code):
    """
    Create or update this subject in every affiliated institute.

    Idempotent on `(university, department code, subject code)`: running it
    again edits the copies rather than making more, which is what makes it
    usable as the edit path too.

    An institute that already has a subject with this code *of its own* is left
    alone and reported. Quietly seizing a row the institute created — and its
    attendance history with it — would be a worse answer than saying so.
    """
    created, updated, skipped = [], [], []
    for department in target_departments(university, department_code):
        existing = Subject.objects.filter(
            department=department, code__iexact=code).first()
        if existing is not None and existing.owner_university_id != university.pk:
            skipped.append(department.institute.name)
            continue
        if existing is None:
            try:
                with transaction.atomic():
                    Subject.objects.create(
                        department=department, code=code,
                        owner_university=university, **fields)
            except IntegrityError:
                # Raced, or differs only by case. Either way the institute has
                # one and this is not the place to adjudicate it.
                skipped.append(department.institute.name)
                continue
            created.append(department.institute.name)
        else:
            for name, value in fields.items():
                setattr(existing, name, value)
            existing.save(update_fields=list(fields))
            updated.append(department.institute.name)
    return {"created": created, "updated": updated, "skipped": skipped}


@transaction.atomic
def push_batch(*, university, label, fields, department_code):
    """The same idea for batches, keyed on the label."""
    created, updated, skipped = [], [], []
    for department in target_departments(university, department_code):
        existing = Batch.objects.filter(
            department=department, label__iexact=label).first()
        if existing is not None and existing.owner_university_id != university.pk:
            skipped.append(department.institute.name)
            continue
        if existing is None:
            try:
                with transaction.atomic():
                    Batch.objects.create(
                        department=department, label=label,
                        owner_university=university, **fields)
            except IntegrityError:
                skipped.append(department.institute.name)
                continue
            created.append(department.institute.name)
        else:
            for name, value in fields.items():
                setattr(existing, name, value)
            existing.save(update_fields=list(fields))
            updated.append(department.institute.name)
    return {"created": created, "updated": updated, "skipped": skipped}


# --------------------------------------------------------------------------- #
#  Siblings, archiving and removal
# --------------------------------------------------------------------------- #
def siblings(row):
    """
    Every copy of this row across the affiliated institutes, including itself.

    Found by natural key rather than by a stored group id: the key is what
    makes two copies the same subject, and deriving it means there is no second
    thing that can fall out of step with the rows.
    """
    university_id = getattr(row, "owner_university_id", None)
    if university_id is None:
        return type(row).objects.filter(pk=row.pk)
    model = type(row)
    match = ({"code__iexact": row.code} if model is Subject
             else {"label__iexact": row.label})
    return model.objects.filter(
        owner_university_id=university_id,
        department__code__iexact=row.department.code,
        **match,
    )


def archive_everywhere(row, *, active):
    """Archive or restore this row in every institute at once."""
    return siblings(row).update(is_active=active)


@transaction.atomic
def remove_everywhere(row):
    """
    Remove the row from every institute, archiving the copies that are in use.

    A copy with students enrolled or teachers allocated is archived rather than
    deleted, exactly as an institute's own row would be — deleting it would
    take real attendance history with it, and a university tidying its syllabus
    is not asking for that. The two outcomes are counted separately so the
    answer can say which happened.
    """
    removed = archived = 0
    for copy in list(siblings(row).select_related("department")):
        in_use = (copy.enrollments.exists() or copy.assignments.exists()
                  if isinstance(copy, Subject) else copy.students.exists())
        if in_use:
            if copy.is_active:
                copy.is_active = False
                copy.save(update_fields=["is_active"])
            archived += 1
        else:
            copy.delete()
            removed += 1
    return {"removed": removed, "archived": archived}


def own_departments(queryset):
    """
    The departments an institute may define batches and subjects under.

    Its own — the autonomous ones, plus grandfathered departments from before
    the catalogue existed. `source` is the whole test, the same test the save
    endpoints apply, so the dropdown lists exactly what the server will accept
    rather than offering a choice that comes back refused.

    Adopted departments are left out because their cohorts and papers are the
    university's to publish. A college adding one beside them would have a
    batch nobody else running the same syllabus has.
    """
    return queryset.filter(source__isnull=True)
