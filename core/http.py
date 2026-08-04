"""Small helpers so every view can speak the same JSON dialect to jQuery."""
from django.core.serializers.json import DjangoJSONEncoder
from django.http import JsonResponse

try:                                    # bson ships with pymongo
    from bson import ObjectId
except ImportError:                     # pragma: no cover - non-Mongo install
    ObjectId = None


class ApiJSONEncoder(DjangoJSONEncoder):
    """
    DjangoJSONEncoder plus Mongo's ObjectId.

    Every view hands the primary key back to the browser as ``{"id": obj.id}``.
    On MongoDB that is an ObjectId, which json.dumps cannot serialise, so
    without this the endpoints raise TypeError. Rendering it as its 24-character
    hex string is loss-free: the browser posts the string back and
    ObjectIdAutoField.to_python turns it into an ObjectId again.
    """

    def default(self, o):
        if ObjectId is not None and isinstance(o, ObjectId):
            return str(o)
        return super().default(o)


def ok(data=None, message="", **extra):
    payload = {"success": True, "message": message, "data": data if data is not None else {}}
    payload.update(extra)
    return JsonResponse(payload, encoder=ApiJSONEncoder)


def fail(message="Something went wrong.", errors=None, status=400, **extra):
    payload = {"success": False, "message": message, "errors": errors or {}, "data": {}}
    payload.update(extra)
    return JsonResponse(payload, status=status, encoder=ApiJSONEncoder)


def form_errors(form):
    """Flatten a Django form's errors into {field: 'first message'}."""
    return {field: errs[0] for field, errs in form.errors.items()}


def is_ajax(request):
    return (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in (request.headers.get("accept") or "")
    )


def client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")
