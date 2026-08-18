"""
The university's own screens: its institutes, and the decisions it owes them.

Kept apart from accounts/views.py because these are the only endpoints where a
university acts *as* a university rather than as a stand-in head. Everything
else it does — students, sessions, reports — runs through the ordinary screens
with a wider scope, which is the point of accounts.scoping.
"""
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from academics import reference
from academics.models import StudentProfile
from core.decorators import role_required
from core.http import fail, form_errors, ok
from core.utils import clean_object_id

from .forms import (
    AUTONOMOUS,
    HeadLoginForm,
    InstituteIdentityForm,
    UniversityInstituteInviteForm,
)
from .affiliations import (
    AffiliationError,
    add_autonomous,
    delink,
    remove,
    set_affiliation,
)
from .institute_approval import approve_institute, reject_institute
from .models import ActivityLog, Discipline, Institute
from .scoping import choose_institute, institutes_for
from .services import invite_institute

UNIVERSITY = "UNIVERSITY"


def _row(institute):
    """One institute, as the university sees it."""
    affiliations = [{
        "discipline": a.discipline,
        "discipline_label": a.get_discipline_display(),
        "university": a.university.short_name or a.university.name
                      if a.university_id else "Autonomous",
        "university_id": str(a.university_id) if a.university_id else "",
        "autonomous": a.university_id is None,
    } for a in institute.affiliations.select_related("university")]
    head = institute.users.filter(role="HEAD").order_by("date_joined").first()
    return {
        "id": str(institute.id),
        "name": institute.name,
        "code": institute.code,
        "email": institute.email,
        "phone": institute.phone,
        # Carried so the edit modal can fill itself from the row it already
        # has, rather than a second fetch per click.
        "website": institute.website,
        "address": institute.address,
        "state": institute.state,
        "district": institute.district,
        "place": " · ".join(x for x in (institute.district, institute.state) if x),
        "status": institute.status,
        "status_label": institute.get_status_display(),
        "rejection_reason": institute.rejection_reason,
        "decided_at": (institute.decided_at.strftime("%d %b %Y, %H:%M")
                       if institute.decided_at else ""),
        "invited": institute.invited_by_id is not None,
        "affiliations": affiliations,
        "head": (head.full_name or head.email) if head else "",
        "head_email": head.email if head else "",
        # An invited head who has not accepted yet. Worth surfacing: the
        # university sent the invitation and is the one who will chase it.
        "head_pending": bool(head and not head.registration_completed),
        # Annotated by api_institutes, which builds the whole list in one
        # query. The single-row callers (the editor) have no annotation, so
        # they count here rather than being made to remember one.
        "students": (institute.students_count
                     if hasattr(institute, "students_count")
                     else StudentProfile.objects.filter(
                         department__institute=institute, is_active=True).count()),
    }


@role_required(UNIVERSITY)
@ensure_csrf_cookie
def institutes_page(request):
    return render(request, "accounts/university_institutes.html", {
        "state_groups": reference.states_grouped(),
        "districts_json": reference.districts_payload(),
        "disciplines": [{"value": v, "label": l} for v, l in Discipline.choices],
        "university": request.user.university,
    })


@role_required(UNIVERSITY)
@require_GET
def api_institutes(request):
    """
    Every institute this university reaches, with the counts the tabs need.

    The counts come from the same queryset as the rows rather than a second
    query, so the badge can never disagree with the list beneath it.
    """
    # `focused=False`: this screen is where the filter is *set*. Narrowing it
    # by the current choice would show a one-row list and no way back.
    qs = (institutes_for(request.user, focused=False)
          .prefetch_related("affiliations__university", "users")
          .annotate(students_count=Count(
              "departments__students",
              filter=Q(departments__students__is_active=True), distinct=True)))
    rows = [_row(i) for i in qs]
    counts = {
        "all": len(rows),
        "pending": sum(1 for r in rows if r["status"] == Institute.Status.PENDING),
        "approved": sum(1 for r in rows if r["status"] == Institute.Status.APPROVED),
        "rejected": sum(1 for r in rows if r["status"] == Institute.Status.REJECTED),
    }
    return ok({"rows": rows, "counts": counts})


def _reachable(request, pk):
    """
    An institute this university may act on, or 404.

    Reach, not focus: approving an institute from the Institutes screen must
    work whatever the filter happens to be set to.
    """
    return get_object_or_404(institutes_for(request.user, focused=False), pk=pk)


@role_required(UNIVERSITY)
@require_POST
def api_institute_approve(request, pk):
    institute = _reachable(request, pk)
    approve_institute(institute=institute, actor=request.user)
    return ok({"status": institute.status},
              message=f"{institute.name} approved. Its head can sign in now.")


@role_required(UNIVERSITY)
@require_POST
def api_institute_reject(request, pk):
    institute = _reachable(request, pk)
    reason = (request.POST.get("reason") or "").strip()
    if not reason:
        # Refused rather than defaulted. A head who is told "rejected" with no
        # reason has nothing to correct and nothing to appeal.
        return fail("Give a reason — it is sent to the institute.")
    reject_institute(institute=institute, actor=request.user, reason=reason)
    return ok({"status": institute.status},
              message=f"{institute.name} rejected. The head has been emailed.")


@role_required(UNIVERSITY)
@require_POST
def api_institute_update(request, pk):
    """
    Correct an institute's name, official email and its head's login.

    This is the other half of the rule in accounts/identity.py: an affiliated
    institute cannot change these itself precisely because they are the
    university's record to keep, so the university has to be able to keep it.

    The head's login is moved, not reissued — no password is set and no session
    is created. `accounts.views._university_may_not_touch` keeps the credential
    side of that line, and it is not relaxed here.
    """
    institute = _reachable(request, pk)
    form = InstituteIdentityForm(request.POST, instance=institute)
    if not form.is_valid():
        return fail("Please correct the highlighted fields.", form_errors(form))

    head = institute.users.filter(role="HEAD").order_by("date_joined").first()
    head_email = (request.POST.get("head_email") or "").strip()
    login_form = None
    if head_email and head is not None and head_email.lower() != head.email.lower():
        login_form = HeadLoginForm({"email": head_email}, user=head)
        if not login_form.is_valid():
            return fail("Please correct the highlighted fields.",
                        {"head_email": login_form.errors["email"][0]})

    with transaction.atomic():
        form.save()
        moved = None
        if login_form is not None:
            moved = head.email
            head.email = login_form.cleaned_data["email"]
            head.save(update_fields=["email"])

    ActivityLog.log(request, action="INSTITUTE_UPDATED",
                    detail=(f"{institute.name}"
                            + (f"; head login {moved} -> {head.email}" if moved else "")))
    message = f"{institute.name} updated."
    if moved:
        message += (f" The head now signs in as {head.email} — tell them, "
                    "because we have not.")
    return ok(_row(institute), message=message)


@role_required(UNIVERSITY)
@require_POST
def api_institute_disciplines(request, pk):
    """
    Add, remove or delink an institute's disciplines.

    One endpoint with an `action` rather than three, because the three are the
    same decision seen from different sides and the rule about whose record it
    is applies identically to all of them — see accounts/affiliations.py.

        affiliate  this university awards it (new, or taken over from nobody)
        delink     the institute becomes autonomous for it; the row stays
        remove     the institute does not teach it at all; the row goes
    """
    institute = _reachable(request, pk)
    action = (request.POST.get("action") or "").strip()
    disciplines = request.POST.getlist("disciplines")
    university = request.user.university

    handlers = {
        "affiliate": lambda: set_affiliation(
            institute=institute, disciplines=disciplines,
            university=university, actor=request.user),
        "delink": lambda: delink(
            institute=institute, disciplines=disciplines,
            university=university, actor=request.user),
        "remove": lambda: remove(
            institute=institute, disciplines=disciplines,
            university=university, actor=request.user),
        # A university may note that an institute runs an autonomous wing
        # without claiming to award it. Same helper the institute's own screen
        # uses, because it is the same act.
        "autonomous": lambda: add_autonomous(
            institute=institute, disciplines=disciplines, actor=request.user),
    }
    if action not in handlers:
        return fail("Choose whether to affiliate, delink, remove or record "
                    "as autonomous.")

    try:
        result = handlers[action]()
    except AffiliationError as exc:
        return fail(str(exc), status=403)

    messages = {
        "affiliate": lambda r: (f"{', '.join(r['changed'])} now affiliated to you."
                                if r["changed"] else
                                f"Already yours: {', '.join(r['unchanged'])}."),
        "delink": lambda r: (f"{institute.name} is now autonomous for "
                             f"{', '.join(r['delinked'])}."
                             if r["delinked"] else
                             f"Already autonomous: {', '.join(r['already'])}."),
        "remove": lambda r: f"Removed {', '.join(r['removed'])}.",
        "autonomous": lambda r: (f"Added {', '.join(r['added'])} as autonomous."
                                 if r["added"] else
                                 f"Already on file: {', '.join(r['existing'])}."),
    }
    row = _row(institute)
    return ok({**result, "row": row, "affiliations": row["affiliations"]},
              message=messages[action](result))


@role_required(UNIVERSITY)
@require_POST
def api_institute_invite(request):
    """
    Register an institute and invite its head.

    Available whether or not this university grants affiliation: inviting is
    not affiliating, and a university that takes only its own institutes still
    needs this.
    """
    university = request.user.university
    form = UniversityInstituteInviteForm(request.POST, university=university)
    if not form.is_valid():
        return fail("Please correct the highlighted fields.", form_errors(form))
    cd = form.cleaned_data
    institute, head, invitation = invite_institute(
        university=university,
        name=cd["institute_name"], code=cd["institute_code"],
        email=cd["institute_email"], head_email=cd["head_email"],
        state=cd["state"], district=cd["district"],
        affiliations=cd["affiliations"],
        invited_by=request.user,
    )
    return ok({"id": str(institute.id)},
              message=f"{institute.name} created. We emailed an invitation to "
                      f"{head.email}.")


@role_required(UNIVERSITY)
@require_POST
def api_switch_institute(request):
    """
    Focus one institute, or clear the focus to see all of them.

    Refused rather than silently reset when the institute is out of reach, so
    a stale bookmark says so instead of quietly showing the wrong thing.
    """
    raw = (request.POST.get("institute") or "").strip()
    institute_id = clean_object_id(raw) if raw else None
    if raw and not institute_id:
        return fail("That is not an institute.", status=400)
    if not choose_institute(request, institute_id):
        return fail("That institute is not one of yours.", status=403)
    return ok({"institute": institute_id or ""})
