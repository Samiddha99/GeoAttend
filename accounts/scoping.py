"""
Which institutes a request may touch.

This is the keystone of the university tier. Before it, every query said
`institute=user.institute` — a single institute, always. A university belongs
to no institute and may reach many, so that expression is wrong for it
everywhere it appears, and the fix is not to special-case each site but to ask
one question here.

**Two ideas, kept separate on purpose.**

`institutes_for(user)` is *reach*: every institute this account may ever see.
For everyone except a university that is exactly one institute.

`active_institute(request)` is *focus*: which of them the screen is currently
showing. A university may focus one institute or leave it on "All institutes",
which is what the dashboard filter changes. Focus never widens reach — it can
only narrow it — so a tampered session cannot reach anything new.

**How focus reaches a query that only has a `user`.** Almost every selector in
this project takes a `user`, not a `request` — `students_qs_for(user)`,
`batches_for(user)`, and a dozen more. Threading a request through all of them
to carry one filter would be a very large change with one failure mode per
call site, and the symptom of missing one is a screen that quietly ignores the
filter. So `UniversityFocusMiddleware` hangs the focused id on the user object
once per request and `institutes_for` reads it from there. This is the same
device `GuardianChildMiddleware` already uses to tell the selectors which child
a guardian is looking at, for the same reason.

The consequence worth knowing: **`institutes_for(user)` is narrowed by default.**
Every data query wants that. The three places that must see the whole reach
regardless — the switcher's own list, its validation, and the Institutes
screen — pass `focused=False`, and they read as unusual because they are.
Getting it wrong in the safe direction shows fewer rows, never more.
"""
from django.db.models import Q

SESSION_INSTITUTE_KEY = "university_institute_id"
FOCUS_ATTR = "_focus_institute_id"


def institutes_for(user, focused=True):
    """
    The institutes this account may reach, as a queryset.

    A university reaches the institutes it affiliates *and* the ones it
    invited — the two are different sets. A university may invite an institute
    it does not affiliate (that is the requirement), and it affiliates
    institutes that registered themselves without an invitation.

    Rejected institutes stay in reach: the university has to be able to see
    what it rejected, and why.

    With `focused` left true (everything except the switcher) the result is
    narrowed to the one institute the university has picked, if any. The
    narrowing is applied *after* reach is computed, so a focus on an institute
    this account cannot reach yields nothing rather than smuggling one in.
    """
    from .models import Institute

    if not getattr(user, "is_authenticated", False):
        return Institute.objects.none()
    if getattr(user, "is_university", False):
        if user.university_id is None:
            return Institute.objects.none()
        reach = Institute.objects.filter(
            Q(affiliations__university_id=user.university_id)
            | Q(invited_by_id=user.university_id)
        ).distinct()
        focus = getattr(user, FOCUS_ATTR, None) if focused else None
        return reach.filter(pk=focus) if focus else reach
    if user.institute_id is None:
        return Institute.objects.none()
    return Institute.objects.filter(pk=user.institute_id)


def set_focus(user, institute_id):
    """
    Record which institute this request is about, for the selectors to read.

    Called by the middleware. Setting it on the user rather than the request is
    deliberate — see the module docstring.
    """
    setattr(user, FOCUS_ATTR, str(institute_id) if institute_id else None)


def institute_ids_for(user, focused=True):
    """The same set as ids — cheaper when it is only used to filter."""
    return list(institutes_for(user, focused=focused).values_list("id", flat=True))


def active_institute(request):
    """
    The single institute a university has focused, or None for "all of them".

    Recomputed against `institutes_for` on every request rather than trusted
    from the session: an institute that leaves this university's reach stops
    being focusable immediately, without waiting for the session to expire.
    """
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return None
    if not getattr(user, "is_university", False):
        return None
    chosen = request.session.get(SESSION_INSTITUTE_KEY)
    if not chosen:
        return None
    return institutes_for(user, focused=False).filter(pk=chosen).first()


def choose_institute(request, institute_id):
    """
    Focus one institute, or clear the focus with a falsy id.

    Returns True when the request was honoured. A university asking for an
    institute outside its reach is refused rather than silently reset, so the
    caller can say so.

    `focused=False` matters here: validating the new choice against the
    *current* focus would mean you could only ever switch to the institute you
    were already on, so the filter would stick on the first thing picked.
    """
    if not institute_id:
        request.session.pop(SESSION_INSTITUTE_KEY, None)
        set_focus(request.user, None)
        return True
    if not institutes_for(request.user, focused=False).filter(
            pk=institute_id).exists():
        return False
    request.session[SESSION_INSTITUTE_KEY] = str(institute_id)
    # Applied to this request too, not just the next one, so a switch that is
    # answered with fresh rows returns rows for the institute just chosen.
    set_focus(request.user, institute_id)
    return True


def visible_institutes(request):
    """
    Reach narrowed by focus — what the current screen should be about.

    Kept as the explicitly request-scoped spelling of `institutes_for`, which
    now narrows by itself.
    """
    return institutes_for(getattr(request, "user", None))


def institute_options(request):
    """Rows for the university's institute filter. [] for anyone else."""
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False) or not user.is_university:
        return []
    current = active_institute(request)
    # `focused=False`: this *is* the filter. Narrowing it by the current choice
    # would leave a dropdown containing only the option already selected.
    return [{
        "id": str(i.id),
        "name": i.name,
        "detail": " · ".join(x for x in (i.district, i.state) if x),
        "status": i.status,
        "current": current is not None and i.pk == current.pk,
    } for i in institutes_for(user, focused=False).order_by("name")]


# --------------------------------------------------------------------------- #
#  Authority
# --------------------------------------------------------------------------- #
def may_administer(user, institute):
    """
    Does this account have head-of-institute authority over `institute`?

    True for that institute's own head, and for a university that reaches it.
    Deliberately not true for a HoD or teacher: they administer a department or
    their own classes, which is a narrower question their own selectors answer.

    An institute that is not approved yet can still be administered by the
    university — that is how it gets approved — but not by its own head, who
    cannot sign in at all until then.
    """
    if not getattr(user, "is_authenticated", False) or institute is None:
        return False
    if getattr(user, "is_university", False):
        # Reach, not focus. Authority is not a view preference: a university
        # that has filtered its dashboard to one institute has not thereby
        # given up the right to act on the others, and a permission check that
        # flips with a dropdown is a permission check nobody can reason about.
        return institutes_for(user, focused=False).filter(pk=institute.pk).exists()
    if getattr(user, "is_head", False):
        return user.institute_id == institute.pk
    return False


def affiliates(university, institute):
    """
    Does this university *affiliate* the institute, as opposed to merely
    having invited it?

    The distinction decides curriculum: a university sets the syllabus for the
    institutes it affiliates. One it merely invited runs its own.
    """
    if university is None or institute is None:
        return False
    return institute.affiliations.filter(university=university).exists()
