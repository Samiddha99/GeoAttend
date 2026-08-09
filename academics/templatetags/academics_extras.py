"""
Template helpers for academics.

`grouped_for_select` exists so that a subject dropdown can group itself from
the plain `subjects` list a view already passes. The alternative — a
`subject_groups` key added to every view that renders a filter bar — is a dozen
edits and a dozen chances to miss one, and a missed one fails quietly: the
dropdown just renders empty.
"""
from django import template

from academics.selectors import grouped_subjects

register = template.Library()


@register.filter
def grouped_for_select(subjects):
    """
    Bundle subjects by degree and then by type, ready for <optgroup>.

    Renamed from `by_subject_type` when degree was added: the old name would
    have described half of what it does, and a filter whose name disagrees with
    its behaviour is worse than a long one.

    Takes whatever the view passed — a queryset, a list, or nothing at all, in
    which case the dropdown is simply empty rather than raising during render.
    """
    if not subjects:
        return []
    return grouped_subjects(subjects)
