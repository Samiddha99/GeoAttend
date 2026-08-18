"""
Who teaches what, to which group — and which students that group contains.

**The group is (batch, section).** A batch alone was enough until sections
existed. A cohort of 180 taught in three rooms by three people needs the finer
key, and an allocation that could only name the batch put all three teachers in
front of all 180 students.

**Null section means the whole batch.** Not "unknown" — *the whole batch*. It is
what every allocation meant before this, and what a college that does not divide
its cohorts still means. Every function here reads it that way, which is what
lets the change land without a backfill and without breaking a college that
never adopts sections.

**Department is derived, never stored.** `subject.department` already says it,
and so does `batch.department`. A third copy on the allocation would be a third
thing to keep in step, and the copy that drifts is the one somebody trusts. The
screens walk department → batch → section → subject because that is how a
timetable is read; the *data* keeps only what cannot be worked out.

**The rule this file exists to hold:** a teacher may open a session for exactly
the group they were allocated, and the students who may mark it are exactly that
group. Those two sentences have to agree, so they are computed from one place —
`students_for` — rather than assembled separately in the session view and the
marking view. Two copies of that query is how a student ends up able to mark a
class they were not in.
"""
from django.db.models import Q

from .models import Section, StudentProfile, TeacherAssignment


class AllocationError(Exception):
    """A refusal with a message meant to be shown to the person."""


def _same_batch(section, batch):
    return section is not None and section.batch_id == batch.pk


def assert_coherent(subject, batch, section=None):
    """
    Raise unless these three describe one real group of students.

    Three ways they can disagree, all of them a picker sending stale ids after
    somebody changed a dropdown:

      * a subject from another department
      * a section from another batch
      * a section whose batch is not the batch named
    """
    if subject.department_id != batch.department_id:
        raise AllocationError(
            f"{subject.code} belongs to {subject.department.code}, and "
            f"{batch.label} to {batch.department.code}. Pick a subject from "
            f"the same department as the batch.")
    if section is not None and not _same_batch(section, batch):
        raise AllocationError(
            f"Section {section.name} belongs to {section.batch.label}, not to "
            f"{batch.label}.")


def students_for(subject, batch, section=None):
    """
    The students an allocation covers — the attendance audience.

    **One definition, used by both halves.** The session view counts this to
    decide who is expected; the marking view asks whether a particular student
    is in it. Computing those separately is how somebody ends up able to mark a
    class they were not in, so there is exactly one query and both call it.

    A null section widens to the whole batch, including students who are not in
    any section. A named section narrows to that section alone — and a student
    with no section is *not* in it, which is the point: they have not been put
    in that room.
    """
    query = StudentProfile.objects.filter(
        batch=batch,
        batch__is_active=True,
        is_active=True,
        enrollments__subject=subject,
        enrollments__is_active=True,
        user__is_active=True,
    )
    if section is not None:
        query = query.filter(section=section)
    return query.select_related("user", "batch", "section").distinct()


def covers(assignment, student):
    """Does this allocation include this student?"""
    if assignment.batch_id != student.batch_id:
        return False
    if assignment.section_id is None:
        return True
    return assignment.section_id == student.section_id


def allocations_for(teacher, *, subject=None, batch=None):
    """This teacher's live allocations, optionally narrowed."""
    query = TeacherAssignment.objects.filter(
        teacher=teacher, is_active=True, batch__is_active=True
    ).select_related("subject", "subject__department", "batch", "section")
    if subject is not None:
        query = query.filter(subject=subject)
    if batch is not None:
        query = query.filter(batch=batch)
    return query


def can_teach(teacher, subject, batch, section=None):
    """
    May this teacher open a session for this exact group?

    **A whole-batch allocation covers any section of it.** Somebody allocated
    "2022-26" teaches all of it, so they may take a register for section A
    alone — splitting a class they already own is not a new permission.

    The reverse is refused: allocated to section A only, they may not open a
    session for the whole batch, because that would put B and C's students in
    front of a teacher nobody gave them.
    """
    query = allocations_for(teacher, subject=subject, batch=batch)
    if section is None:
        # The whole batch was asked for, so only a whole-batch allocation will
        # do.
        return query.filter(section__isnull=True).exists()
    return query.filter(Q(section=section) | Q(section__isnull=True)).exists()


def assert_can_teach(teacher, subject, batch, section=None):
    """The same question, with the message the API should refuse with."""
    if can_teach(teacher, subject, batch, section):
        return
    where = batch.label + (f" · {section.name}" if section is not None else "")
    # Named separately because the two refusals send somebody to different
    # places: one is "ask for this class", the other is "you have a narrower
    # allocation than you think".
    narrower = allocations_for(teacher, subject=subject, batch=batch).filter(
        section__isnull=False)
    if section is None and narrower.exists():
        names = ", ".join(a.section.name for a in narrower)
        raise AllocationError(
            f"You teach {subject.code} to section(s) {names} of {batch.label}, "
            f"not to the whole batch. Choose a section.")
    raise AllocationError(
        f"You are not assigned to teach {subject.code} to {where}.")


def resolve_pairs(pairs, department):
    """
    Turn the browser's `[{subject_id, batch_id, section_id}]` into objects.

    Returns `(resolved, error)`. Everything is validated against `department`,
    so a payload naming another department's subject is refused here rather
    than becoming an allocation nobody can explain.

    `section_id` absent or blank means the whole batch — the browser sends "" for
    that, and it must not be confused with a bad id.
    """
    from .models import Batch, Subject
    from core.utils import clean_object_id

    subject_ids = {clean_object_id(p.get("subject_id")) for p in pairs}
    batch_ids = {clean_object_id(p.get("batch_id")) for p in pairs}
    subjects = {str(s.id): s for s in Subject.objects.select_related(
        "department").filter(id__in=subject_ids, department=department)}
    batches = {str(b.id): b for b in Batch.objects.filter(
        id__in=batch_ids, department=department, is_active=True)}
    section_ids = {clean_object_id(p.get("section_id"))
                   for p in pairs if (p.get("section_id") or "").strip()}
    sections = {str(s.id): s for s in Section.objects.select_related(
        "batch").filter(id__in=section_ids)}

    resolved = []
    for pair in pairs:
        subject = subjects.get(str(pair.get("subject_id")))
        batch = batches.get(str(pair.get("batch_id")))
        if subject is None or batch is None:
            return None, ("One of the selected subjects or batches is not in "
                          "your department, or the batch is archived.")
        raw_section = (pair.get("section_id") or "").strip()
        section = sections.get(str(raw_section)) if raw_section else None
        if raw_section and section is None:
            return None, "One of the selected sections no longer exists."
        try:
            assert_coherent(subject, batch, section)
        except AllocationError as exc:
            return None, str(exc)
        resolved.append((subject, batch, section))

    # A duplicate in one payload would otherwise be written twice and hit the
    # uniqueness constraint mid-loop, failing a save that was only ever
    # ambiguous. Caught here so the message names the problem.
    keys = [(s.pk, b.pk, sec.pk if sec else None) for s, b, sec in resolved]
    if len(set(keys)) != len(keys):
        return None, "The same subject, batch and section appears twice."
    return resolved, None


def set_allocations(teacher, resolved, *, actor=None):
    """
    Make this teacher's live allocations exactly `resolved`.

    Anything not resubmitted is deactivated rather than deleted: an allocation
    is what a term of attendance hangs off, and removing the row would orphan
    the sessions taken under it.
    """
    keep = set()
    for subject, batch, section in resolved:
        obj, _ = TeacherAssignment.objects.update_or_create(
            teacher=teacher, subject=subject, batch=batch, section=section,
            defaults={"assigned_by": actor, "is_active": True},
        )
        keep.add(obj.id)
    TeacherAssignment.objects.filter(teacher=teacher).exclude(
        id__in=keep).update(is_active=False)
    return keep


def label(assignment):
    """`DSA · 2022-26 · A` — how an allocation reads in a list."""
    where = assignment.batch.label
    if assignment.section_id:
        where += f" · {assignment.section.name}"
    return f"{assignment.subject.code} · {where}"


def groups_for(teacher):
    """
    Every group this teacher may take a register for, ready for a picker.

    A whole-batch allocation is expanded into one entry per section, plus the
    batch itself where a college has students outside any section. That is the
    list a teacher walking into a room actually chooses from — they pick "2022-26
    · B", not "2022-26, and then narrow it".
    """
    rows = []
    for assignment in allocations_for(teacher):
        subject, batch = assignment.subject, assignment.batch
        base = {
            "assignment_id": str(assignment.id),
            "department": subject.department.name,
            "department_code": subject.department.code,
            "department_id": str(subject.department_id),
            "batch": batch.label,
            "batch_id": str(batch.pk),
            "subject": f"{subject.code} — {subject.name}",
            "subject_code": subject.code,
            "subject_id": str(subject.pk),
        }
        if assignment.section_id is not None:
            rows.append({**base,
                         "section": assignment.section.name,
                         "section_id": str(assignment.section_id)})
            continue
        sections = list(Section.objects.filter(batch=batch, is_active=True))
        for section in sections:
            rows.append({**base, "section": section.name,
                         "section_id": str(section.pk)})
        # The whole batch stays on offer when there are no sections at all, and
        # also when there are — a college part-way through sectioning has
        # students in neither, and hiding the batch would hide them.
        rows.append({**base, "section": "", "section_id": ""})
    rows.sort(key=lambda r: (r["department_code"], r["batch"], r["section"],
                             r["subject_code"]))
    return rows
