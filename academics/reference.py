"""
Static reference data: states, districts, disciplines and affiliating bodies.

**Why states and districts are not models.** There are 36 states and 727
districts, they change roughly never, and nothing in the app needs to join on
them or attach anything to them — an institute stores the two names and that is
the whole relationship. Making them tables would buy foreign keys nobody
queries, at the cost of a migration, a seed step and a fixture in every test.
They live in `data/states.json` and are read once at import.

Universities *are* models, because they hold accounts, grant affiliation and
own curricula. `data/universities.json` is only the seed list — the authority
is the database once seeded.

The JSON files are generated from the source lists and committed. Regenerating
them is a deliberate act, not something that happens at runtime.
"""
import json
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"


# --------------------------------------------------------------------------- #
#  States and districts
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def _states():
    with open(DATA / "states.json", encoding="utf-8") as handle:
        return json.load(handle)["states"]


def states_grouped():
    """
    States and union territories, as two <optgroup>s.

    Grouped rather than flat because the list is 36 long and a person looking
    for "Delhi" should not have to know it is a union territory to find it in
    an alphabetical run of states.
    """
    groups = [
        {"label": "States",
         "states": [s["state"] for s in _states() if not s["union_territory"]]},
        {"label": "Union Territories",
         "states": [s["state"] for s in _states() if s["union_territory"]]},
    ]
    return [g for g in groups if g["states"]]


def all_states():
    return [s["state"] for s in _states()]


@lru_cache(maxsize=1)
def _districts_by_state():
    return {s["state"]: s["districts"] for s in _states()}


def districts_for(state):
    """The districts of one state, or [] for anything unrecognised."""
    return list(_districts_by_state().get((state or "").strip(), []))


def is_valid_place(state, district):
    """
    Both together, or neither. A district is only meaningful inside its state,
    so validating them separately would accept "Kerala / Bhopal".
    """
    if not state:
        return False
    return district in _districts_by_state().get(state.strip(), [])


def districts_payload():
    """
    {state: [districts]} for the browser, so the District dropdown can refill
    without a round trip. About 30 KB — worth it to avoid a request on every
    change of a field people fill once.
    """
    return _districts_by_state()


# --------------------------------------------------------------------------- #
#  Affiliating bodies
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def _seed():
    with open(DATA / "universities.json", encoding="utf-8") as handle:
        return json.load(handle)


def seed_universities():
    """
    {discipline_code: [names]} from the shipped list.

    Only used to populate the University table. Several bodies appear under
    more than one discipline — JNTU Anantapur grants both engineering and
    pharmacy affiliation — which is why a university holds a *set* of
    disciplines rather than one.
    """
    return _seed()["universities"]


def seed_discipline_labels():
    """{label: code} exactly as written in the source list."""
    return _seed()["disciplines"]
