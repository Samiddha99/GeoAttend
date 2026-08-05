"""
The feedback form itself.

Kept in code rather than in a table on purpose. A question bank would need its
own CRUD, its own permissions and its own migration story, and nobody has asked
to edit these — but every stored answer has to stay readable, so the set is
versioned and each form records which version it was built from. Change the
questions by adding a version; never by editing one that has answers.

Two rules shaped the wording.

**Every question is asked positively.** "Did the teacher arrive on time?" and
not "was the teacher late?" — a negative stem makes an agreeing student pick the
option that looks like a complaint, and it makes the results read backwards
until someone notices.

**Scores are per option, not per position.** Most questions run best-to-worst,
so position would do; pace does not. "Too fast" and "too slow" are both wrong
and sit at opposite ends, with the right answer in the middle. Attaching the
score to the option keeps that honest and keeps the averages meaningful.
"""

# 2: "Was the language used easy to follow?" became a question about the
#    language of instruction.
# 3: that question became scored. The wording and the option values did not
#    change, but what an answer is worth did — and a version pins the scoring
#    as much as the wording, so an average computed later can still say which
#    rules produced it.
CURRENT_VERSION = 3

# The order groups appear in, which is the order a student reads them. Declared
# separately so the question list can be written in whatever order makes sense
# to edit without changing what anyone sees.
GROUP_ORDER = ["Subject", "Delivery", "Communication", "Conduct"]


def _scale(*labels):
    """Options running best to worst, scored evenly from 1.0 down to 0."""
    if len(labels) == 1:
        return [{"value": labels[0].lower(), "label": labels[0], "score": 1.0}]
    step = 1.0 / (len(labels) - 1)
    return [
        {"value": label.lower().replace(" ", "_"), "label": label,
         "score": round(1.0 - i * step, 3)}
        for i, label in enumerate(labels)
    ]


QUESTIONS = [
    {
        "key": "punctuality",
        "text": "Did the teacher start the class on time?",
        "group": "Conduct",
        "options": _scale("Always", "Usually", "Sometimes", "Rarely"),
    },
    {
        "key": "subject_knowledge",
        "text": "How strong was the teacher's command of the subject?",
        "group": "Subject",
        "options": _scale("Excellent", "Good", "Fair", "Needs improvement"),
    },
    {
        "key": "depth",
        "text": "How deeply were the concepts covered?",
        "group": "Subject",
        "options": _scale("Very deep", "Deep enough", "Fairly basic", "Too shallow"),
    },
    {
        "key": "explanation",
        "text": "How easy was the explanation to follow?",
        "group": "Delivery",
        "options": _scale("Very easy", "Easy", "Somewhat hard", "Hard"),
    },
    {
        "key": "real_world",
        "text": "How well were the concepts linked to real-world examples?",
        "group": "Subject",
        "options": _scale("Very well", "Well", "Occasionally", "Not really"),
    },
    {
        "key": "queries",
        "text": "How well were students' questions answered?",
        "group": "Delivery",
        "options": _scale("Very well", "Well", "Partly", "Not really"),
    },
    {
        "key": "audible",
        "text": "Was the teacher clearly audible from your seat?",
        "group": "Communication",
        "options": _scale("Always", "Mostly", "Sometimes", "Rarely"),
    },
    {
        "key": "pace",
        "text": "How was the speaking pace?",
        "group": "Communication",
        # Bipolar: the best answer is in the middle, so the scores are not
        # ordered. A student who says "too slow" is not agreeing with one who
        # says "too fast", and averaging them as if they were would report a
        # comfortable pace nobody experienced.
        "bipolar": True,
        "options": [
            {"value": "too_slow", "label": "Too slow", "score": 0.2},
            {"value": "a_bit_slow", "label": "A little slow", "score": 0.6},
            {"value": "just_right", "label": "Just right", "score": 1.0},
            {"value": "a_bit_fast", "label": "A little fast", "score": 0.6},
            {"value": "too_fast", "label": "Too fast", "score": 0.2},
        ],
    },
    {
        "key": "english",
        "text": "How much of the class was delivered in English?",
        "group": "Communication",
        # Scored as a descending scale, so more English rates higher and the
        # options carry the same colours as everything else.
        #
        # That is an institutional policy encoded as a number, not a fact about
        # teaching: a teacher who explains a hard idea in the language their
        # students think in may be doing the better job. It counts toward the
        # overall score, so it moves a teacher's figures. Set every score back
        # to None to keep the question and drop the judgement — the colours go
        # neutral with it, because tone is derived from score.
        "options": [
            {"value": "entirely", "label": "Entirely in English", "score": 1.0},
            {"value": "mostly", "label": "Mostly in English", "score": 0.667},
            {"value": "mixed", "label": "An equal mix of English and another language",
             "score": 0.333},
            {"value": "mostly_other", "label": "Mostly in another language", "score": 0.0},
        ],
    },
    {
        "key": "board_work",
        "text": "Was the writing and drawing on the board clear?",
        "group": "Delivery",
        "options": _scale("Very clear", "Clear", "Partly clear", "Hard to read")
                   + [{"value": "not_used", "label": "Board not used",
                       # Scored None so it is counted and reported but left out
                       # of the average: a teacher who used slides should not be
                       # marked down for board work that never happened.
                       "score": None}],
    },
]

def _tone(option, bipolar):
    """
    A colour band for one option, derived from its score.

    Derived rather than hand-assigned so the colour can never disagree with the
    number behind it — a green option worth 0.2 would be worse than no colour
    at all. Unscored options stay neutral: they are not good or bad, they are
    just what happened.

    Worth knowing: colouring the choices probably nudges answers a little
    towards the green end, because picking a red one feels like an accusation.
    Watch the distributions after a term and reconsider if everything is
    suspiciously positive.
    """
    score = option["score"]
    if score is None:
        return "neutral"
    if score >= 0.85:
        return "good"
    if score >= 0.55:
        return "fair"
    if score >= 0.30:
        return "warn"
    return "bad"


# Sorted by group so all the subject questions sit together, then delivery, and
# so on. A stable sort keeps the declared order within each group.
QUESTIONS.sort(key=lambda q: GROUP_ORDER.index(q["group"])
               if q["group"] in GROUP_ORDER else len(GROUP_ORDER))

for _question in QUESTIONS:
    for _option in _question["options"]:
        _option["tone"] = _tone(_option, _question.get("bipolar", False))

QUESTION_INDEX = {q["key"]: q for q in QUESTIONS}
GROUPS = [g for g in GROUP_ORDER if any(q["group"] == g for q in QUESTIONS)]


def option_of(key, value):
    question = QUESTION_INDEX.get(key)
    if question is None:
        return None
    return next((o for o in question["options"] if o["value"] == value), None)


def score_of(key, value):
    """0..1 for one answer, or None when the option is deliberately unscored."""
    option = option_of(key, value)
    return None if option is None else option["score"]


def as_payload():
    """The form as the browser needs it — text, groups, labels and colours."""
    return [{
        "key": q["key"],
        "text": q["text"],
        "group": q["group"],
        "bipolar": q.get("bipolar", False),
        "descriptive": q.get("descriptive", False),
        "options": [{"value": o["value"], "label": o["label"], "tone": o["tone"]}
                    for o in q["options"]],
    } for q in QUESTIONS]
