import logging

from django.conf import settings
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from core.decorators import deny_guardian_page, guardian_readonly
from core.http import client_ip, fail, form_errors, ok
from core.utils import normalise_email

from .devices import fingerprint_for, login_gate
from .emails import send_otp
from .forms import (
    AUTONOMOUS,
    ChangePasswordForm,
    ForgotPasswordForm,
    HeadLoginForm,
    InstituteIdentityForm,
    InstituteSignupForm,
    InstituteInviteAcceptForm,
    InviteAcceptForm,
    LoginForm,
    ProfileForm,
    ResetPasswordForm,
    UniversityIdentityForm,
    UniversitySignupForm,
)
from . import face_service
from .face import FaceError
from .affiliations import (
    AffiliationError,
    add_autonomous,
    available_disciplines,
    contents_of,
    remove_own,
    rows_for,
)
from . import coverage
from .identity import (
    affiliating_universities,
    identity_lock_reason,
    own_name_lock_reason,
    is_autonomous,
    may_edit_identity,
)
from .institute_approval import sign_in_blocked_reason
from .models import ActivityLog, Discipline, EmailOTP, Invitation, University, User
from .services import (
    activate_invitee,
    create_institute_and_head,
    create_university_and_admin,
    unlink_device,
)

log = logging.getLogger("geoattend")

SIGNUP_SESSION_KEY = "signup_otp_id"


def _university_may_not_touch(actor, target):
    """
    True when a signed-in university is reaching for an institute account's
    credentials.

    A university has a head's read and write reach over institute *data*. It
    does not get the head's login. The distinction matters because the two look
    similar from the outside and only one of them is reversible: data can be
    corrected, an account takeover cannot be undone.
    """
    if not getattr(actor, "is_authenticated", False):
        return False
    if not getattr(actor, "is_university", False):
        return False
    return target is not None and target.institute_id is not None


def pending_signup_otp(request, purpose=None):
    """
    The OTP this browser is part-way through, or None.

    The session holds the id as a string (an ObjectId is not JSON serialisable,
    so it cannot go in directly). Looking it up needs care: a session that
    predates a change, or one a user has tampered with, can hold something that
    is not a valid id at all, and ObjectIdAutoField raises ValidationError
    rather than simply not matching. That should read as "start again", not 500.
    """
    otp_id = request.session.get(SIGNUP_SESSION_KEY)
    if not otp_id:
        return None
    filters = {"id": otp_id}
    if purpose is not None:
        filters["purpose"] = purpose
    try:
        return EmailOTP.objects.filter(**filters).first()
    except (ValidationError, ValueError, TypeError):
        # A tampered or stale session id lands here, and "start again" is the
        # right answer for it. But anything else that raises while loading the
        # row lands here too and is reported as an expired session, which sends
        # the user round a loop that cannot succeed and leaves no trace. Log it
        # so the next one is diagnosable from the server, not from guesswork.
        log.exception("could not load signup OTP %r — reported to the user as "
                      "an expired session", otp_id)
        request.session.pop(SIGNUP_SESSION_KEY, None)
        return None


# --------------------------------------------------------------------------- #
#  Pages (thin — everything else happens over AJAX)
# --------------------------------------------------------------------------- #
@ensure_csrf_cookie
def login_page(request):
    return render(request, "accounts/login.html", {
        "form": LoginForm(),
        "next": request.GET.get("next", ""),
    })


@ensure_csrf_cookie
def signup_page(request):
    from academics import reference

    return render(request, "accounts/signup.html", {
        "form": InstituteSignupForm(),
        "state_groups": reference.states_grouped(),
        # Shipped to the browser so the District dropdown refills without a
        # round trip on a field people fill once. ~30 KB.
        "districts_json": reference.districts_payload(),
        "disciplines": [{"value": v, "label": l} for v, l in Discipline.choices],
    })


@ensure_csrf_cookie
def university_signup_page(request):
    from academics import reference

    return render(request, "accounts/university_signup.html", {
        "form": UniversitySignupForm(),
        "state_groups": reference.states_grouped(),
        "districts_json": reference.districts_payload(),
        "disciplines": [{"value": v, "label": l} for v, l in Discipline.choices],
    })


@require_GET
def api_affiliating_bodies(request):
    """
    The bodies that grant affiliation for one discipline.

    Public, because it feeds the signup form of an institute that does not have
    an account yet. It exposes only what the shipped list already publishes —
    which universities affiliate which subjects — and nothing about who is
    registered with whom.
    """
    discipline = (request.GET.get("discipline") or "").strip().upper()
    if discipline not in Discipline.values:
        return ok({"rows": []})
    rows = [{"id": str(u.id), "name": u.name, "short_name": u.short_name}
            for u in University.objects.filter(
                disciplines__discipline=discipline,
                grants_affiliation=True, is_active=True).distinct()]
    return ok({"rows": rows, "discipline": discipline})


@require_GET
def api_seeded_universities(request):
    """
    Unclaimed bodies from the shipped list, for the university signup form.

    Picking one claims that row instead of creating a near-duplicate — which is
    what stops "Anna University" and "Anna Univ." becoming two accounts that
    cannot see each other's institutes.
    """
    rows = [{"id": str(u.id), "name": u.name,
             "disciplines": sorted(u.disciplines.values_list("discipline", flat=True))}
            for u in University.objects.filter(
                is_seeded=True, claimed_at__isnull=True).prefetch_related("disciplines")]
    return ok({"rows": rows})


@ensure_csrf_cookie
def forgot_password_page(request):
    return render(request, "accounts/forgot_password.html", {"form": ForgotPasswordForm()})


@ensure_csrf_cookie
def invite_page(request, token):
    invitation = Invitation.objects.filter(token=token).select_related(
        "institute", "department"
    ).first()
    is_head_invite = (invitation is not None
                      and invitation.role == User.Role.HEAD
                      and invitation.institute_id is not None)
    context = {
        "invitation": invitation, "token": token,
        "is_head_invite": is_head_invite,
        # A teacher is shown their name rather than asked for it — see
        # accounts/forms.InviteAcceptForm.
        "is_teacher_invite": (invitation is not None
                              and invitation.role == User.Role.TEACHER),
        "form": (InstituteInviteAcceptForm(institute=invitation.institute)
                 if is_head_invite
                 else InviteAcceptForm(
                     role=invitation.role if invitation else None)),
    }
    if invitation is None:
        context["error"] = "This invitation link is not valid."
    elif invitation.status == Invitation.Status.ACCEPTED:
        context["error"] = "This invitation has already been used. Please sign in instead."
    elif invitation.status == Invitation.Status.REVOKED:
        context["error"] = "This invitation was revoked by your institute."
    elif invitation.is_expired:
        context["error"] = "This invitation has expired. Ask your institute to resend it."
    return render(request, "accounts/invite_accept.html", context)


@login_required
@ensure_csrf_cookie
def complete_profile(request):
    if request.user.registration_completed:
        return redirect("dashboard:home")
    return render(request, "accounts/complete_profile.html")


# --------------------------------------------------------------------------- #
#  Face enrolment
# --------------------------------------------------------------------------- #
@login_required
@deny_guardian_page
@ensure_csrf_cookie
def face_capture_page(request):
    """
    Where a student is held until their face is on file.

    Deliberately reachable only by someone who still needs it: a student who
    has enrolled is sent to their dashboard rather than being offered a second
    capture, because re-enrolment is staff's to authorise.
    """
    if not face_service.needs_enrolment(request.user):
        return redirect("dashboard:home")
    return render(request, "accounts/face_capture.html", {
        "poses": face_service.POSES,
    })


@login_required
@guardian_readonly
@require_POST
def api_face_enrol(request):
    """Three frames in, one enrolment out — or one message saying why not."""
    uploads = {pose: request.FILES.get(pose.lower()) for pose in face_service.POSES}
    try:
        face_service.enrol(user=request.user, uploads=uploads, request=request)
    except FaceError as exc:
        # Logged as well as returned. A refusal is a 400 whose reason lives
        # only in the response body, so without this line the server log says
        # "Bad Request" and nothing else — which is no help at all when a
        # student rings up to say enrolment will not work.
        log.warning("Face enrolment refused for %s: %s (%s) %s",
                    request.user.email, exc.code, exc.message,
                    exc.detail or "")
        # `pose` lets the page send the student back to the one angle that was
        # refused instead of making them repeat all three. Absent for refusals
        # that belong to no single frame, such as "these are not the same
        # person" — and the page starts over in that case, which is right.
        return fail(exc.message, code=exc.code, status=400,
                    pose=exc.detail.get("pose", ""))
    return ok({"redirect": reverse("dashboard:home")},
              message="Face captured. You're all set.")


@login_required
@deny_guardian_page
@ensure_csrf_cookie
def profile_page(request):
    profile = None
    if request.user.is_student:
        profile = (
            getattr(request.user, "student_profile", None)
        )
    # The organisation this account belongs to. Shown to everyone — a teacher
    # seeing which institute they are signed in to is useful and harmless — but
    # only editable per accounts/identity.py.
    institute = request.user.institute
    university = request.user.university if request.user.is_university else None
    return render(request, "accounts/profile.html", {
        "form": ProfileForm(instance=request.user),
        "password_form": ChangePasswordForm(request.user),
        "student_profile": profile,
        "institute": institute,
        "university": university,
        "institute_form": (InstituteIdentityForm(instance=institute)
                           if institute else None),
        "university_form": (UniversityIdentityForm(instance=university)
                            if university else None),
        "may_edit_identity": may_edit_identity(request.user, institute),
        "identity_lock_reason": identity_lock_reason(request.user, institute),
        # The sentence, or None — the template uses it as both the flag and the
        # explanation, so the two cannot disagree about whether the field is
        # locked.
        "own_name_locked": own_name_lock_reason(request.user),
        "is_autonomous": is_autonomous(institute),
        "affiliating": list(affiliating_universities(institute)) if institute else [],
        # Feature 3 and 4: what the institute teaches, who awards it, and what
        # is left to add. Offered to the head only — a teacher seeing the list
        # is fine, a teacher changing it is not, and the endpoint enforces it.
        "affiliation_rows": rows_for(institute),
        "available_disciplines": (available_disciplines(institute)
                                  if institute else []),
        # The same two lists from the university's side. Separate keys rather
        # than one reused pair, because the rows carry different columns and a
        # template that has to ask which kind it is holding is a template that
        # will eventually get it wrong.
        "coverage_rows": coverage.rows_for(university),
        "coverage_available": coverage.available(university),
        "can_self_unlink": (
            not request.user.is_student
            or settings.ATTENDANCE["ALLOW_STUDENT_SELF_DEVICE_RESET"]
        ),
    })


@require_GET
def logout_view(request):
    ActivityLog.log(request, action="LOGOUT")
    logout(request)
    return redirect("accounts:login")


# --------------------------------------------------------------------------- #
#  AJAX: login
# --------------------------------------------------------------------------- #
@require_POST
def api_login(request):
    form = LoginForm(request.POST)
    if not form.is_valid():
        return fail("Please correct the highlighted fields.", form_errors(form))
    email = normalise_email(form.cleaned_data["email"])
    user = authenticate(request, username=email, password=form.cleaned_data["password"])
    if user is None:
        existing = User.objects.filter(email=email).first()
        if existing and not existing.registration_completed:
            return fail(
                "Your account has not been activated yet. Please open the invitation "
                "link we emailed you to set your password."
            )
        ActivityLog.objects.create(action="LOGIN_FAILED", detail=email, ip=client_ip(request) or None)
        return fail("Invalid email or password.")
    if not user.is_active:
        return fail("Your account has been deactivated. Please contact your institute.")

    # An institute that named an affiliating university waits for it. Checked
    # after the password so this never reveals whether an account exists.
    blocked = sign_in_blocked_reason(user)
    if blocked:
        ActivityLog.objects.create(
            action="LOGIN_BLOCKED_PENDING", detail=email,
            ip=client_ip(request) or None)
        return fail(blocked, status=403, code="INSTITUTE_NOT_APPROVED")

    # --- one device per student ------------------------------------------- #
    fingerprint, device_error = login_gate(user, request, request.POST.get("device_hash", ""))
    if device_error:
        ActivityLog.objects.create(
            actor=user, institute=user.institute, action="LOGIN_DEVICE_BLOCKED",
            detail=f"{user.email} tried to sign in from an unregistered device",
            meta={"attempted": fingerprint[:16], "registered": user.device_id[:16]},
            ip=client_ip(request) or None,
        )
        return fail(device_error, status=403, code="DEVICE_MISMATCH")

    login(request, user)
    user.bind_device(fingerprint)      # no-op once a device is already recorded
    if not form.cleaned_data.get("remember"):
        request.session.set_expiry(0)
    ActivityLog.log(request, action="LOGIN", detail=user.role)

    nxt = request.POST.get("next") or ""
    if not user.registration_completed:
        redirect_to = reverse("accounts:complete_profile")
    elif nxt.startswith("/"):
        redirect_to = nxt
    else:
        redirect_to = reverse("dashboard:home")
    return ok({"redirect": redirect_to}, message=f"Welcome back, {user.short_name}!")


# --------------------------------------------------------------------------- #
#  AJAX: institute signup (2 steps + OTP)
# --------------------------------------------------------------------------- #
@require_POST
def api_signup_start(request):
    form = InstituteSignupForm(request.POST)
    if not form.is_valid():
        return fail("Please correct the highlighted fields.", form_errors(form))
    cd = form.cleaned_data
    payload = {
        "institute_name": cd["institute_name"],
        "institute_code": cd["institute_code"],
        "institute_email": cd["institute_email"],
        "phone": cd.get("phone", ""),
        "website": cd.get("website", ""),
        "address": cd.get("address", ""),
        "head_name": cd["head_name"],
        "head_email": cd["head_email"],
        "head_phone": cd.get("head_phone", ""),
        "password": cd["password1"],
        "state": cd["state"],
        "district": cd["district"],
        # `str(pk)`, not `pk`. This lands in a JSONField, and on MongoDB a raw
        # primary key is a BSON ObjectId — which survives the write into a
        # subdocument and then blows up on the way back out, long after the
        # request that caused it. A string round-trips on every backend and is
        # what `university_id=` wants anyway.
        #
        # None still means autonomous for that discipline.
        "affiliations": {d: (str(u.pk) if u else None)
                         for d, u in (cd.get("affiliations") or {}).items()},
    }
    otp, code = EmailOTP.issue(cd["head_email"], EmailOTP.Purpose.INSTITUTE_SIGNUP, payload)
    send_otp(cd["head_email"], code, "finish creating your institute account")
    request.session[SIGNUP_SESSION_KEY] = str(otp.id)
    data = {"email": cd["head_email"], "ttl": settings.OTP_TTL_MINUTES}
    if settings.DEBUG:
        data["debug_code"] = code  # convenience while developing
    return ok(data, message=f"We emailed a 6-digit code to {cd['head_email']}.")


@require_POST
def api_university_signup_start(request):
    form = UniversitySignupForm(request.POST)
    if not form.is_valid():
        return fail("Please correct the highlighted fields.", form_errors(form))
    cd = form.cleaned_data
    existing = cd.get("existing_university")
    payload = {
        "university_name": cd["university_name"],
        "short_name": cd.get("short_name", ""),
        "university_code": cd["university_code"],
        "university_email": cd["university_email"],
        "phone": cd.get("phone", ""),
        "website": cd.get("website", ""),
        "address": cd.get("address", ""),
        "state": cd["state"],
        "district": cd["district"],
        "disciplines": list(cd["disciplines"]),
        "grants_affiliation": bool(cd.get("grants_affiliation")),
        "admin_name": cd["admin_name"],
        "admin_email": cd["admin_email"],
        "admin_phone": cd.get("admin_phone", ""),
        "password": cd["password1"],
        "existing": str(existing.pk) if existing else "",
    }
    otp, code = EmailOTP.issue(cd["admin_email"],
                               EmailOTP.Purpose.INSTITUTE_SIGNUP, payload)
    send_otp(cd["admin_email"], code, "finish creating your university account")
    request.session[SIGNUP_SESSION_KEY] = str(otp.id)
    data = {"email": cd["admin_email"], "ttl": settings.OTP_TTL_MINUTES}
    if settings.DEBUG:
        data["debug_code"] = code
    return ok(data, message=f"We emailed a 6-digit code to {cd['admin_email']}.")


@require_POST
def api_university_signup_verify(request):
    otp = pending_signup_otp(request, purpose=EmailOTP.Purpose.INSTITUTE_SIGNUP)
    if otp is None:
        return fail("Your signup session expired. Please start again.", status=410)
    good, message = otp.verify(request.POST.get("code", ""))
    if not good:
        return fail(message)

    payload = dict(otp.payload)
    payload["existing_university"] = (
        University.objects.filter(pk=payload.get("existing") or "").first()
        if payload.get("existing") else None)
    university, admin = create_university_and_admin(payload)
    request.session.pop(SIGNUP_SESSION_KEY, None)
    user = authenticate(request, username=admin.email, password=payload["password"])
    if user:
        login(request, user)
    return ok({"redirect": reverse("dashboard:home")},
              message=f"Welcome, {university.short_name or university.name}.")


@require_POST
def api_signup_verify(request):
    otp = pending_signup_otp(request, purpose=EmailOTP.Purpose.INSTITUTE_SIGNUP)
    if otp is None:
        return fail("Your signup session expired. Please start again.", status=410)
    good, message = otp.verify(request.POST.get("code", ""))
    if not good:
        return fail(message)
    institute, head = create_institute_and_head(otp.payload)
    request.session.pop(SIGNUP_SESSION_KEY, None)

    # An institute awaiting approval is not signed in — its head cannot use the
    # app yet, and dropping them on a dashboard that immediately bounces them
    # would be worse than telling them plainly.
    blocked = sign_in_blocked_reason(head)
    if blocked:
        return ok({"redirect": reverse("accounts:login"), "pending": True},
                  message=blocked)

    user = authenticate(request, username=head.email, password=otp.payload["password"])
    if user:
        login(request, user)
    return ok(
        {"redirect": reverse("dashboard:home")},
        message=f"{institute.name} is ready. Welcome aboard, {head.short_name}!",
    )


@require_POST
def api_signup_resend(request):
    otp = pending_signup_otp(request)
    if otp is None:
        return fail("Your signup session expired. Please start again.", status=410)
    if (timezone.now() - otp.created_at).total_seconds() < settings.OTP_RESEND_COOLDOWN_SEC:
        wait = settings.OTP_RESEND_COOLDOWN_SEC - int((timezone.now() - otp.created_at).total_seconds())
        return fail(f"Please wait {wait}s before requesting another code.")
    new_otp, code = EmailOTP.issue(otp.email, otp.purpose, otp.payload)
    send_otp(otp.email, code, "finish creating your institute account")
    request.session[SIGNUP_SESSION_KEY] = str(new_otp.id)
    data = {"debug_code": code} if settings.DEBUG else {}
    return ok(data, message="A fresh code is on its way.")


# --------------------------------------------------------------------------- #
#  AJAX: invitation acceptance
# --------------------------------------------------------------------------- #
@require_POST
def api_invite_accept(request, token):
    invitation = Invitation.objects.filter(token=token).select_related(
        "institute", "department"
    ).first()
    if invitation is None:
        return fail("This invitation link is not valid.", status=404)
    if invitation.status == Invitation.Status.ACCEPTED:
        return fail("This invitation has already been used. Please sign in.", status=409)
    if invitation.status == Invitation.Status.REVOKED:
        return fail("This invitation was revoked.", status=403)
    if invitation.is_expired:
        invitation.status = Invitation.Status.EXPIRED
        invitation.save(update_fields=["status"])
        return fail("This invitation has expired. Please ask for a new one.", status=410)

    # A head accepting a university's invitation fills in the institute too —
    # its email, phone, website and address are its own business, and the
    # university that created the record could only have guessed at them.
    # State and district are absent from that form on purpose, not disabled:
    # an input that is not rendered cannot be posted.
    is_head_invite = (invitation.role == User.Role.HEAD
                      and invitation.institute_id is not None)
    if is_head_invite:
        form = InstituteInviteAcceptForm(request.POST,
                                         institute=invitation.institute)
    else:
        form = InviteAcceptForm(request.POST, role=invitation.role)
    if not form.is_valid():
        return fail("Please correct the highlighted fields.", form_errors(form))

    if is_head_invite:
        institute = invitation.institute
        institute.email = form.cleaned_data["institute_email"]
        institute.phone = form.cleaned_data.get("phone", "")
        institute.website = form.cleaned_data.get("website", "")
        institute.address = form.cleaned_data.get("address", "")
        institute.save(update_fields=["email", "phone", "website", "address"])

    user = activate_invitee(
        invitation,
        # Absent for a teacher — the form does not ask, because their name is
        # already on file and PAN-verified. `activate_invitee` keeps whatever
        # is stored when this is blank.
        full_name=form.cleaned_data.get("full_name", ""),
        phone=form.cleaned_data.get(
            "phone_head" if is_head_invite else "phone", ""),
        raw_password=form.cleaned_data["password1"],
    )
    auth_user = authenticate(request, username=user.email, password=form.cleaned_data["password1"])
    if auth_user:
        login(request, auth_user)
        # The device they activate on becomes their registered one.
        auth_user.bind_device(fingerprint_for(request, request.POST.get("device_hash", "")))
    return ok(
        {"redirect": reverse("dashboard:home")},
        message=f"Your account is ready, {user.short_name}!",
    )


# --------------------------------------------------------------------------- #
#  AJAX: password reset via OTP
# --------------------------------------------------------------------------- #
@require_POST
def api_forgot_start(request):
    form = ForgotPasswordForm(request.POST)
    if not form.is_valid():
        return fail("Please enter a valid email address.", form_errors(form))
    email = normalise_email(form.cleaned_data["email"])
    # Guardian accounts are excluded, not just unlikely to match. Their email is
    # synthetic (…@guardian.invalid, undeliverable) and they hold an unusable
    # password, so a reset could never complete — but leaving them in the query
    # means one code path where "set a password" is even attempted for an
    # account whose whole design is not having one.
    user = User.objects.filter(
        email=email, registration_completed=True
    ).exclude(role=User.Role.GUARDIAN).first()
    # A signed-in university must not start a password reset for one of its
    # institutes. Reading an institute's data is the whole point of the role;
    # taking over the head's login is not, and a reset code is a takeover.
    if user is not None and _university_may_not_touch(request.user, user):
        user = None
    data = {"email": email}
    if user:
        otp, code = EmailOTP.issue(email, EmailOTP.Purpose.PASSWORD_RESET)
        send_otp(email, code, "reset your password")
        if settings.DEBUG:
            data["debug_code"] = code
    # Never reveal whether the account exists.
    return ok(data, message="If that email is registered, a reset code is on its way.")


@require_POST
def api_forgot_confirm(request):
    form = ResetPasswordForm(request.POST)
    if not form.is_valid():
        return fail("Please correct the highlighted fields.", form_errors(form))
    email = normalise_email(form.cleaned_data["email"])
    otp = (
        EmailOTP.objects.filter(
            email=email, purpose=EmailOTP.Purpose.PASSWORD_RESET, is_used=False
        )
        .order_by("-created_at")
        .first()
    )
    if otp is None:
        return fail("No active reset request found. Please start again.", status=410)
    good, message = otp.verify(form.cleaned_data["code"])
    if not good:
        return fail(message)
    # Belt and braces: no code can ever be issued for a guardian above, but the
    # two halves of this flow are separate endpoints and should not depend on
    # each other's filtering to stay safe.
    user = User.objects.filter(email=email).exclude(role=User.Role.GUARDIAN).first()
    if user is None:
        return fail("Account not found.", status=404)
    user.set_password(form.cleaned_data["password1"])
    user.save(update_fields=["password"])
    ActivityLog.log(actor=user, action="PASSWORD_RESET")
    return ok({"redirect": reverse("accounts:login")}, message="Password updated. You can sign in now.")


# --------------------------------------------------------------------------- #
#  AJAX: profile
# --------------------------------------------------------------------------- #
@login_required
@guardian_readonly
@require_POST
def api_profile_update(request):
    form = ProfileForm(request.POST, instance=request.user)
    if not form.is_valid():
        return fail("Please correct the highlighted fields.", form_errors(form))
    form.save()
    return ok({"full_name": request.user.full_name}, message="Profile updated.")


@login_required
@guardian_readonly
@require_POST
def api_organisation_update(request):
    """
    Save the organisation's name and official email from the profile page.

    A university edits its own record. An institute's head edits theirs only if
    the institute is autonomous — the rule lives in accounts/identity.py and is
    checked here rather than inferred from the form, so a head who posts the
    fields anyway is refused rather than obeyed.
    """
    if request.user.is_university:
        university = request.user.university
        if university is None:
            return fail("This account is not linked to a university.", status=403)
        form = UniversityIdentityForm(request.POST, instance=university)
        if not form.is_valid():
            return fail("Please correct the highlighted fields.", form_errors(form))
        form.save()
        ActivityLog.log(request, action="UNIVERSITY_UPDATED", detail=university.name)
        return ok({"name": university.name}, message="University details saved.")

    institute = request.user.institute
    reason = identity_lock_reason(request.user, institute)
    if reason:
        return fail(reason, status=403)

    form = InstituteIdentityForm(request.POST, instance=institute)
    if not form.is_valid():
        return fail("Please correct the highlighted fields.", form_errors(form))
    form.save()
    ActivityLog.log(request, action="INSTITUTE_UPDATED", detail=institute.name)
    return ok({"name": institute.name}, message="Institute details saved.")


@login_required
@guardian_readonly
@require_POST
def api_login_email_update(request):
    """
    Move this account's own login to a different address.

    Open to every head and every university administrator, affiliated or not.

    **This is deliberately not gated on `identity_lock_reason`, and it was
    once.** Grouping the login with the name and the official email was wrong.
    Those two are the university's record — they appear on the degrees — but a
    login is not a record of anything, it is how one person reaches their own
    account. Locking it meant a head at an affiliated institute whose email had
    changed had to ask an outside body for permission to keep signing in, which
    is not a permission any university asked for or wants to administer.

    The university can still move it from the Institutes screen. Both being
    able to is the point: whoever notices first can fix it.

    The session is refreshed so the change does not sign the person out
    mid-edit.
    """
    if not (request.user.is_university or request.user.is_head):
        return fail("Only the head of the institute can change this.",
                    status=403)

    form = HeadLoginForm(request.POST, user=request.user)
    if not form.is_valid():
        return fail("Please correct the highlighted fields.", form_errors(form))

    previous = request.user.email
    request.user.email = form.cleaned_data["email"]
    request.user.save(update_fields=["email"])
    update_session_auth_hash(request, request.user)
    ActivityLog.log(request, action="LOGIN_EMAIL_CHANGED",
                    detail=f"{previous} -> {request.user.email}")
    return ok({"email": request.user.email},
              message="Your login email has been changed. Use it next time you sign in.")


@login_required
@guardian_readonly
@require_POST
def api_add_disciplines(request):
    """
    Record disciplines this institute teaches, under its own authority.

    Open to the head of *any* institute, affiliated or not: saying "we also
    teach pharmacy, and nobody else awards it" is a statement about itself.
    Naming a university is the part it cannot do alone, and there is
    deliberately no way to do so from here — the university has its own screen
    for that.
    """
    institute = request.user.institute
    if institute is None or not request.user.is_head:
        return fail("Only the head of the institute can change this.", status=403)
    try:
        result = add_autonomous(
            institute=institute,
            disciplines=request.POST.getlist("disciplines"),
            actor=request.user)
    except AffiliationError as exc:
        return fail(str(exc))

    message = (f"Added {', '.join(result['added'])} as autonomous."
               if result["added"] else "Nothing to add.")
    if result["existing"]:
        message += (f" Already on file: {', '.join(result['existing'])} — "
                    "left exactly as they were.")
    return ok(result, message=message)


@login_required
@guardian_readonly
@require_GET
def api_discipline_contents(request, code):
    """
    What is inside a discipline, so the removal modal can say so.

    A GET because it changes nothing and the modal opens on it. The counts are
    the whole point of the screen: "archive it all" against a number is a
    decision, against a blank it is a gamble.
    """
    institute = request.user.institute
    if institute is None or not request.user.is_head:
        return fail("Only the head of the institute can see this.", status=403)
    if code not in Discipline.values:
        return fail("Unknown discipline.")
    row = institute.affiliations.filter(discipline=code).first()
    if row is None:
        return fail("That discipline is not on file.")
    return ok({
        "discipline": code,
        "label": row.get_discipline_display(),
        "autonomous": row.university_id is None,
        "university": (row.university.short_name or row.university.name
                       if row.university_id else ""),
        "counts": contents_of(institute, code),
    })


@login_required
@guardian_readonly
@require_POST
def api_remove_discipline(request):
    """
    Unlist a discipline, archiving or keeping what is inside it.

    The choice is required rather than defaulted. Defaulting to "keep" would
    quietly leave a wing of the college live after the head thought they had
    closed it; defaulting to "archive" would hide three years of records
    because someone clicked through a modal. Neither is a default anyone should
    inherit.
    """
    institute = request.user.institute
    if institute is None or not request.user.is_head:
        return fail("Only the head of the institute can change this.", status=403)

    code = (request.POST.get("discipline") or "").strip()
    choice = (request.POST.get("contents") or "").strip()
    if choice not in ("archive", "keep"):
        return fail("Choose whether to archive what is in this discipline or "
                    "leave it in place.")
    try:
        result = remove_own(institute=institute, discipline=code,
                            archive=(choice == "archive"), actor=request.user)
    except AffiliationError as exc:
        return fail(str(exc), status=403)

    counts = result["counts"]
    if result["archived"]:
        touched = ", ".join(
            f"{n} {noun}" for noun, n in (
                ("departments", counts["departments"]),
                ("batches", counts["batches"]),
                ("students", counts["students"]),
                ("teachers", counts["teachers"]))
            if n)
        message = (f"{result['label']} removed"
                   + (f", and {touched} archived." if touched else ".")
                   + " Nothing was deleted — re-add the discipline to bring it "
                     "all back.")
    else:
        message = (f"{result['label']} removed. Its departments and everything "
                   "in them are untouched and still yours to manage.")
    return ok(result, message=message)


# --------------------------------------------------------------------------- #
#  The university's own disciplines — the twin of the three endpoints above.
#  See accounts/coverage.py for what "withdrawing" does and does not touch.
# --------------------------------------------------------------------------- #
def _university_of(request):
    """The university this account administers, or None."""
    if not request.user.is_university or request.user.university_id is None:
        return None
    return request.user.university


@login_required
@require_POST
def api_add_coverage(request):
    """Record disciplines this university awards."""
    university = _university_of(request)
    if university is None:
        return fail("Only a university account can change this.", status=403)
    try:
        result = coverage.add(university=university,
                              disciplines=request.POST.getlist("disciplines"),
                              actor=request.user)
    except coverage.CoverageError as exc:
        return fail(str(exc))

    message = (f"Now awarding {', '.join(result['added'])}."
               if result["added"] else "Nothing to add.")
    if result["existing"]:
        message += (f" Already on file: {', '.join(result['existing'])} — "
                    "left exactly as they were.")
    return ok(result, message=message)


@login_required
@require_GET
def api_coverage_contents(request, code):
    """
    What is inside a covered discipline, so the removal modal can say so.

    A GET because it changes nothing and the modal opens on it. The counts are
    the whole point of the screen: "archive it all" against a number is a
    decision, against a blank it is a gamble.
    """
    university = _university_of(request)
    if university is None:
        return fail("Only a university account can see this.", status=403)
    if code not in Discipline.values:
        return fail("Unknown discipline.")
    row = university.disciplines.filter(discipline=code).first()
    if row is None:
        return fail("That discipline is not on file.")
    return ok({
        "discipline": code,
        "label": row.get_discipline_display(),
        "counts": coverage.contents_of(university, code),
        "only_one": university.disciplines.count() == 1,
    })


@login_required
@require_POST
def api_remove_coverage(request):
    """
    Stop awarding a discipline.

    The archive-or-keep choice is required rather than defaulted, for the same
    reason it is on the institute screen: neither answer is one a person should
    inherit by clicking through. It governs this university's own catalogue
    only — the affiliated colleges are delinked either way, because an
    affiliation to a body that no longer awards the degree is a claim nobody
    can back.
    """
    university = _university_of(request)
    if university is None:
        return fail("Only a university account can change this.", status=403)

    code = (request.POST.get("discipline") or "").strip()
    choice = (request.POST.get("contents") or "").strip()
    if choice not in ("archive", "keep"):
        return fail("Choose whether to archive the departments, batches and "
                    "subjects you publish for this discipline, or leave them "
                    "in place.")
    try:
        result = coverage.remove(university=university, discipline=code,
                                 archive=(choice == "archive"),
                                 actor=request.user)
    except coverage.CoverageError as exc:
        return fail(str(exc), status=403)

    counts = result["counts"]
    parts = []
    if result["archived"]:
        touched = ", ".join(
            f"{n} {noun}" for noun, n in (
                ("departments", counts["departments"]),
                ("batches", counts["batches"]),
                ("subjects", counts["subjects"]))
            if n)
        if touched:
            parts.append(f"{touched} archived in your catalogue")
    elif counts["departments"]:
        parts.append("your catalogue for it is untouched")
    if result["delinked"]:
        parts.append(f"{result['delinked']} institute(s) now autonomous in it, "
                     "with their departments, students and attendance intact")
    message = (f"{result['label']} withdrawn"
               + (f" — {'; '.join(parts)}." if parts else ".")
               + " Nothing was deleted.")
    return ok(result, message=message)


@login_required
@guardian_readonly
@require_POST
def api_change_password(request):
    form = ChangePasswordForm(request.user, request.POST)
    if not form.is_valid():
        return fail("Please correct the highlighted fields.", form_errors(form))
    request.user.set_password(form.cleaned_data["password1"])
    request.user.save(update_fields=["password"])
    update_session_auth_hash(request, request.user)
    ActivityLog.log(request, action="PASSWORD_CHANGED")
    return ok(message="Password changed successfully.")


@login_required
@guardian_readonly
@require_POST
def api_reset_device(request):
    """
    Release your own device binding.

    Disabled for students by default: letting them reset on demand would make the
    one-device rule meaningless, since anyone could unlink, hand their login to a
    friend, and re-bind afterwards.  Staff unlink on their behalf instead, from
    Manage → Students.  Set ALLOW_STUDENT_SELF_DEVICE_RESET=True to allow it.
    """
    if request.user.is_student and not settings.ATTENDANCE["ALLOW_STUDENT_SELF_DEVICE_RESET"]:
        return fail(
            "Only your department can unlink your device. Contact your teacher or "
            "department office and they will release it for you.",
            status=403,
        )
    if not unlink_device(request.user, actor=request.user, request=request):
        return fail("There is no device linked to your account.")
    return ok(message="Device unlinked. The next device you use will be bound to your account.")


@require_GET
def api_check_email(request):
    """Live availability check used by the signup form."""
    email = normalise_email(request.GET.get("email", ""))
    taken = bool(email) and User.objects.filter(email=email).exists()
    return ok({"available": not taken, "email": email})
