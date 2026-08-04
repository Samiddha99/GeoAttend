from django.conf import settings
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from core.http import client_ip, fail, form_errors, ok
from core.utils import normalise_email

from .devices import fingerprint_for, login_gate
from .emails import send_otp
from .forms import (
    ChangePasswordForm,
    ForgotPasswordForm,
    InstituteSignupForm,
    InviteAcceptForm,
    LoginForm,
    ProfileForm,
    ResetPasswordForm,
)
from .models import ActivityLog, EmailOTP, Invitation, User
from .services import activate_invitee, create_institute_and_head, unlink_device

SIGNUP_SESSION_KEY = "signup_otp_id"


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
    return render(request, "accounts/signup.html", {"form": InstituteSignupForm()})


@ensure_csrf_cookie
def forgot_password_page(request):
    return render(request, "accounts/forgot_password.html", {"form": ForgotPasswordForm()})


@ensure_csrf_cookie
def invite_page(request, token):
    invitation = Invitation.objects.filter(token=token).select_related(
        "institute", "department"
    ).first()
    context = {"invitation": invitation, "form": InviteAcceptForm(), "token": token}
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


@login_required
@ensure_csrf_cookie
def profile_page(request):
    profile = None
    if request.user.is_student:
        profile = (
            getattr(request.user, "student_profile", None)
        )
    return render(request, "accounts/profile.html", {
        "form": ProfileForm(instance=request.user),
        "password_form": ChangePasswordForm(request.user),
        "student_profile": profile,
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
    }
    otp, code = EmailOTP.issue(cd["head_email"], EmailOTP.Purpose.INSTITUTE_SIGNUP, payload)
    send_otp(cd["head_email"], code, "finish creating your institute account")
    request.session[SIGNUP_SESSION_KEY] = str(otp.id)
    data = {"email": cd["head_email"], "ttl": settings.OTP_TTL_MINUTES}
    if settings.DEBUG:
        data["debug_code"] = code  # convenience while developing
    return ok(data, message=f"We emailed a 6-digit code to {cd['head_email']}.")


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

    form = InviteAcceptForm(request.POST)
    if not form.is_valid():
        return fail("Please correct the highlighted fields.", form_errors(form))

    user = activate_invitee(
        invitation,
        full_name=form.cleaned_data["full_name"],
        phone=form.cleaned_data.get("phone", ""),
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
    user = User.objects.filter(email=email, registration_completed=True).first()
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
    user = User.objects.filter(email=email).first()
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
@require_POST
def api_profile_update(request):
    form = ProfileForm(request.POST, instance=request.user)
    if not form.is_valid():
        return fail("Please correct the highlighted fields.", form_errors(form))
    form.save()
    return ok({"full_name": request.user.full_name}, message="Profile updated.")


@login_required
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
