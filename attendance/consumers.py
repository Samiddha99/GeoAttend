"""
The live face-verification socket.

Why a socket at all: capture, submit, wait, fail, capture again is a loop the
student has to drive by hand, and each turn costs a round trip plus their
attention. Here the browser streams frames and the server answers each one, so a
match lands the moment the light is right rather than the moment the student
happens to press a button.

Two things shape everything below.

**A socket does not make inference faster.** Embedding one frame costs a
noticeable fraction of a second on a shared CPU. Sent 10 frames a second, the
server does not go faster — it builds a queue of frames nobody is waiting for
any more. So exactly one frame is in flight per socket: the browser sends, the
server answers, and only then does the browser send another. A process-wide
semaphore caps how many of those run at once across all students.

**The browser never decides anything.** It cannot say "matched", and it cannot
mark attendance. It sends pixels; the server compares them to the stored vectors
and, if it is satisfied, writes the record itself.
"""
import asyncio
import json
import logging

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.conf import settings
from django.utils import timezone

log = logging.getLogger("geoattend")

# Shared by every socket in the process. The bottleneck is CPU: past this many
# frames at once, more concurrency just makes everyone slower together.
_inference_slots = None


def inference_slots():
    global _inference_slots
    if _inference_slots is None:
        _inference_slots = asyncio.Semaphore(
            int(settings.FACE.get("LIVE_MAX_CONCURRENT", 2)))
    return _inference_slots


class FaceMarkConsumer(AsyncWebsocketConsumer):
    """One student, one class, one attempt at being recognised."""

    async def connect(self):
        self.user = self.scope.get("user")
        self.token = self.scope["url_route"]["kwargs"]["token"]
        self.ticket = None
        self.embeddings = []
        self.frames = 0
        self.best = 0.0
        self.finished = False

        if self.user is None or not self.user.is_authenticated:
            # 4401: a close code the page can tell apart from a network drop,
            # so it can send the student to sign in rather than retrying.
            await self.close(code=4401)
            return
        await self.accept()
        await self.send_json({
            "state": "ready",
            "hint": "Hold your face in the frame.",
            "fallback_after": int(settings.FACE.get("LIVE_FALLBACK_AFTER_SEC", 45)),
        })

    async def disconnect(self, code):
        # Nothing to clean up: an unspent ticket simply expires. Deliberately
        # not marking it used — a dropped connection is usually a bad network,
        # and burning the ticket would punish the student for it.
        return

    async def receive(self, text_data=None, bytes_data=None):
        if self.finished:
            return

        if text_data is not None:
            await self._handle_control(text_data)
            return

        if bytes_data is None:
            return
        if self.ticket is None:
            await self.send_json({"state": "error",
                                  "hint": "This attempt is not authorised."})
            await self.close(code=4403)
            return

        limit = int(settings.FACE.get("LIVE_MAX_FRAME_BYTES", 400 * 1024))
        if len(bytes_data) > limit:
            # The browser crops to the face and scales down before sending, so
            # anything this large is a client that is not doing its share.
            await self.send_json({"state": "frame_too_big",
                                  "hint": "Frame too large."})
            return

        self.frames += 1
        if self.frames > int(settings.FACE.get("LIVE_MAX_FRAMES", 400)):
            await self._give_up("Too many attempts without a match.")
            return

        # One at a time, process-wide. Waiting here is the backpressure: the
        # browser is holding off on its next frame until this one is answered.
        async with inference_slots():
            verdict = await self._match(bytes_data)

        if verdict.get("state") == "matched":
            await self._succeed(verdict)
            return

        self.best = max(self.best, float(verdict.get("score") or 0))
        verdict["attempts"] = self.frames
        verdict["best"] = round(self.best, 3)
        await self.send_json(verdict)

    # ------------------------------------------------------------------ steps
    async def _handle_control(self, text_data):
        try:
            message = json.loads(text_data)
        except ValueError:
            return
        action = message.get("action")

        if action == "start":
            await self._start(message.get("ticket", ""))
        elif action == "give_up":
            await self._request_manual(message.get("reason", ""))

    async def _start(self, token):
        """Redeem the ticket that says the geo-fence has already been passed."""
        from .live import load_attempt

        result = await sync_to_async(load_attempt)(
            user=self.user, session_token=self.token, ticket_token=token)
        if result.get("error"):
            await self.send_json({"state": "error", "hint": result["error"]})
            await self.close(code=4403)
            return

        self.ticket = result["ticket"]
        self.embeddings = result["embeddings"]
        await self.send_json({"state": "verifying",
                              "hint": "Look at the camera."})

    async def _match(self, data):
        from accounts import face as face_engine

        def run():
            try:
                return face_engine.match_frame(data, self.embeddings)
            except face_engine.FaceError as exc:
                return {"state": "unclear", "hint": exc.message}
            except Exception:
                log.exception("Live face match failed")
                return {"state": "unclear", "hint": "Could not read that frame."}

        # thread_sensitive=False: this is pure CPU with no ORM in it, so it runs
        # in the thread pool instead of serialising behind every other database
        # call in the process.
        return await sync_to_async(run, thread_sensitive=False)()

    async def _succeed(self, verdict):
        from .live import complete_mark

        self.finished = True
        result = await sync_to_async(complete_mark)(
            ticket=self.ticket, score=float(verdict.get("score") or 0),
            liveness=verdict.get("liveness"))
        await self.send_json({
            "state": "marked" if result.get("ok") else "error",
            "hint": result.get("message", ""),
            "score": verdict.get("score"),
            "distance": result.get("distance"),
        })
        await self.close()

    async def _give_up(self, reason):
        self.finished = True
        await self.send_json({"state": "exhausted", "hint": reason})
        await self.close()

    async def _request_manual(self, reason):
        """
        The student asks the teacher to look up.

        Only reachable because the geo-fence already passed, so they are in the
        room — the open question is identity, and the teacher standing in front
        of them can settle it faster than any model.
        """
        from .live import request_manual_mark

        if self.ticket is None:
            await self.close(code=4403)
            return
        self.finished = True
        result = await sync_to_async(request_manual_mark)(
            ticket=self.ticket, reason=reason or "Face not recognised",
            attempts=self.frames, best_score=self.best)
        await self.send_json({"state": "asked_teacher",
                              "hint": result.get("message", "")})
        await self.close()

    async def send_json(self, payload):
        await self.send(text_data=json.dumps(payload))
