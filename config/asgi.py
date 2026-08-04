import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# get_asgi_application() must run before anything imports a model: Channels'
# routing pulls in consumers, which pull in models, and Django is not ready yet.
from django.core.asgi import get_asgi_application  # noqa: E402

django_asgi_app = get_asgi_application()

from channels.auth import AuthMiddlewareStack  # noqa: E402
from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402

from config.routing import websocket_urlpatterns  # noqa: E402

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    # OriginValidator is the WebSocket equivalent of CSRF: without it any page
    # on any site could open a socket carrying the student's session cookie.
    "websocket": AllowedHostsOriginValidator(
        AuthMiddlewareStack(URLRouter(websocket_urlpatterns))
    ),
})
