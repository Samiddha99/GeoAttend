from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    # pages
    path("login/", views.login_page, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("signup/", views.signup_page, name="signup"),
    path("forgot/", views.forgot_password_page, name="forgot"),
    path("invite/<str:token>/", views.invite_page, name="invite"),
    path("complete/", views.complete_profile, name="complete_profile"),
    path("face/", views.face_capture_page, name="face_capture"),
    path("profile/", views.profile_page, name="profile"),
    # ajax
    path("api/login/", views.api_login, name="api_login"),
    path("api/signup/start/", views.api_signup_start, name="api_signup_start"),
    path("api/signup/verify/", views.api_signup_verify, name="api_signup_verify"),
    path("api/signup/resend/", views.api_signup_resend, name="api_signup_resend"),
    path("api/invite/<str:token>/accept/", views.api_invite_accept, name="api_invite_accept"),
    path("api/forgot/start/", views.api_forgot_start, name="api_forgot_start"),
    path("api/forgot/confirm/", views.api_forgot_confirm, name="api_forgot_confirm"),
    path("api/profile/update/", views.api_profile_update, name="api_profile_update"),
    path("api/profile/password/", views.api_change_password, name="api_change_password"),
    path("api/profile/reset-device/", views.api_reset_device, name="api_reset_device"),
    path("api/face/enrol/", views.api_face_enrol, name="api_face_enrol"),
    path("api/check-email/", views.api_check_email, name="api_check_email"),
]
