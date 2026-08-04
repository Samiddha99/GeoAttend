"""
WebSocket URL map.

Kept apart from config/urls.py because the two are matched by different
protocols and nothing is shared between them; putting a `ws://` route in the
HTTP urlconf reads as if a browser could GET it.
"""
from django.urls import path

from attendance.consumers import FaceMarkConsumer

websocket_urlpatterns = [
    # The token identifies the attendance session; the ticket that authorises
    # the attempt is sent as the first message, never in the URL, because URLs
    # end up in logs and proxies.
    path("ws/attendance/mark/<str:token>/", FaceMarkConsumer.as_asgi()),
]
