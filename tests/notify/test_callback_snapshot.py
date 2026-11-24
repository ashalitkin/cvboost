"""Telegram keyboards: callback_data are frozen (constants taken from main before #70b),
button labels are English (#70)."""

from __future__ import annotations

import pytest

from cvboost.notify import format as F

A = 4321

# state -> [(label, callback_data)] per row. callback_data copied from main, never regenerated.
EXPECTED = {
    "initial": [
        [("Interested", "i:4321"), ("No", "n:4321"), ("Prepare CV", "cv:4321")],
    ],
    "reasons": [
        [("Stack", "r:4321:stack"), ("Rate", "r:4321:rate")],
        [("Location", "r:4321:location"), ("Other", "r:4321:other")],
        [("Back", "b:4321")],
    ],
    "interested": [
        [("Interested ✓", "x:4321"), ("Prepare CV", "cv:4321"), ("Undo", "u:4321")],
    ],
    "rejected:stack": [[("No: Stack", "x:4321"), ("Undo", "u:4321")]],
    "rejected:rate": [[("No: Rate", "x:4321"), ("Undo", "u:4321")]],
    "rejected:location": [[("No: Location", "x:4321"), ("Undo", "u:4321")]],
    "rejected:other": [[("No: Other", "x:4321"), ("Undo", "u:4321")]],
    "cv_queued": [[("CV: queued", "x:4321"), ("Undo", "u:4321")]],
}


@pytest.mark.parametrize("state", list(EXPECTED))
def test_keyboard_snapshot(state):
    kb = F.keyboard(A, state)
    rows = [[(b["text"], b["callback_data"]) for b in row] for row in kb["inline_keyboard"]]
    assert rows == EXPECTED[state]


def test_callback_regex_and_ops_unchanged():
    assert F.OPS == ("i", "n", "r", "cv", "u", "b", "x")
    assert F.REASONS == ("stack", "rate", "location", "other")
    assert (
        F.CALLBACK_RE.pattern == r"^(i|n|r|cv|u|b|x):(\d{1,10})(?::(stack|rate|location|other))?$"
    )
