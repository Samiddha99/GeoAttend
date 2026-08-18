"""
Class feedback in the admin.

**Read this before widening anything here.** The whole design of this app is
that staff see feedback without seeing who gave it — `feedback/services.py`
has a test that walks every staff payload looking for anything that identifies
a respondent. But the responses *are* stored against a student, because a
student can read their own submission back.

So the admin is the one place that link is visible, and it is registered
carefully:

* `FeedbackResponse` is read-only, and the student is not in `list_display` or
  the search fields. Finding "what did Asha say about DSA" should take
  deliberate effort, not a search box.
* Nothing here is editable. Correcting a student's opinion is not a repair.

The alternative — leaving these unregistered — was tempting, but a support
question like "this form went to 40 students and shows 3 responses, why?" is
unanswerable without them.
"""
from django.contrib import admin

from .models import FeedbackForm, FeedbackRecipient, FeedbackResponse


class RecipientInline(admin.TabularInline):
    model = FeedbackRecipient
    extra = 0
    fields = ("student", "responded_at")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(FeedbackForm)
class FeedbackFormAdmin(admin.ModelAdmin):
    list_display = ("session", "created_by", "sent_count", "response_count",
                    "question_version", "created_at", "expires_at")
    list_filter = ("question_version", "session__subject__department__institute")
    search_fields = ("session__subject__code", "session__batch__label",
                     "created_by__email")
    date_hierarchy = "created_at"
    autocomplete_fields = ("session", "created_by")
    inlines = [RecipientInline]
    readonly_fields = ("created_at", "sent_count")

    @admin.display(description="Responses")
    def response_count(self, obj):
        return obj.responses.count()


@admin.register(FeedbackRecipient)
class FeedbackRecipientAdmin(admin.ModelAdmin):
    """
    Who was asked, and whether they have answered.

    Safe to show the student here: this records that a form was *sent* to them
    and whether they replied — not what they said. That distinction is the
    whole reason recipients and responses are separate tables.
    """

    list_display = ("form", "student", "responded_at")
    list_filter = ("form__session__subject__department__institute",)
    search_fields = ("student__user__email", "student__user__full_name")
    autocomplete_fields = ("form", "student")
    readonly_fields = ("responded_at",)


@admin.register(FeedbackResponse)
class FeedbackResponseAdmin(admin.ModelAdmin):
    """
    Read-only, and deliberately awkward to trace back to a person.

    The student column is omitted from the list and from the search fields.
    The link exists in the row and a determined superuser can open it — that is
    unavoidable, since the column is there — but it is not something you can
    stumble into or grep for.
    """

    list_display = ("form", "rating", "submitted_at")
    list_filter = ("rating", "form__session__subject__department__institute")
    date_hierarchy = "submitted_at"
    # Deliberately not searchable by student. See the note above.
    search_fields = ("form__session__subject__code",)
    readonly_fields = [f.name for f in FeedbackResponse._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
