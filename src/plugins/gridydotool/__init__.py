"""Grid mouse control through ydotool, without the GNOME Shell extension.

Routes the grid conversation, computes cell bounds, and delegates rendering to
`_overlay.py` (a GTK4 layer-shell surface in a gi-capable interpreter) while
`ydotool` moves and clicks the pointer at kernel level. The zone math, the
trigger words and the number tables mirror `mousegrid` on purpose: this plugin
shadows the same phrases, so one utterance must land on the same pixel whichever
backend answers.
"""

import logging
import re

from easyspeak.core.i18n import translator

logger = logging.getLogger(__name__)

_ = translator(__file__)

NAME = "gridydotool"
DESCRIPTION = "Voice-controlled mouse grid (overlay + kernel-level injection)"
PRIORITY = 0

COMMANDS = [
    _("grid/mouse/pointer/cursor - show mouse grid"),
    _("1-9 or chains like '3 7 5' - zoom to zone(s)"),
    _("click - left click"),
    _("right click / double click / middle click"),
    _("close/cancel - hide the grid"),
    _("again - reopen at the last spot"),
]

# Recognition data copied from mousegrid (asserted equal by the test suite):
# Whisper mishears "grid" as any of these often enough that the extension
# backend tunes a whole set, and a backend that hears fewer words than the one
# it shadows is a regression in the user's eyes, not a simplification.
GRID_TRIGGERS = {
    "grid",
    "grit",
    "grip",
    "great",
    "greed",
    "grade",
    "grad",
    "grill",
    "guild",
    "greet",
    "grin",
    "green",
    "grey",
    "gray",
    "grab",
    "grand",
    "greek",
    "grew",
    "mouse",
    "pointer",
    "cursor",
    "crosshair",
}

WORD_TO_NUM = {
    "one": 1,
    "won": 1,
    "wan": 1,
    "two": 2,
    "to": 2,
    "too": 2,
    "tu": 2,
    "three": 3,
    "tree": 3,
    "free": 3,
    "four": 4,
    "for": 4,
    "fore": 4,
    "five": 5,
    "six": 6,
    "sex": 6,
    "seven": 7,
    "eight": 8,
    "ate": 8,
    "nine": 9,
    "nein": 9,
}

ZONE_MAP = {
    1: (0, 0),
    2: (1, 0),
    3: (2, 0),
    4: (0, 1),
    5: (1, 1),
    6: (2, 1),
    7: (0, 2),
    8: (1, 2),
    9: (2, 2),
}


def subdivide(bounds, zone):
    """Return the ninth of `bounds` named by `zone` (1-9, row-major).

    Integer thirds, exactly as `mousegrid.update_grid` computes them, including
    its truncation: `w // 3` of a 1000-wide cell is 333, not 333.33, and the two
    backends must agree on that. mousegrid's off-monitor safety clamp is absent
    here because it cannot fire: x + w stays inside the monitor by construction.
    """
    x, y, w, h = bounds
    zone_w, zone_h = w // 3, h // 3
    col, row = ZONE_MAP[zone]
    return (x + col * zone_w, y + row * zone_h, zone_w, zone_h)


def center(bounds):
    """Return the (x, y) centre of `bounds`."""
    x, y, w, h = bounds
    return (x + w // 2, y + h // 2)


def parse_number_sequence(text):
    """Extract every zone number from an utterance, in order.

    `"3 7 5"` and `"three seven five"` both give `[3, 7, 5]`; the word table
    carries Whisper's usual mishearings, mirroring mousegrid's parser so the same
    utterance chains the same zooms on either backend.
    """
    lowered = text.lower()
    for word, num in WORD_TO_NUM.items():
        lowered = re.sub(rf"\b{word}\b", str(num), lowered)
    return [int(char) for char in lowered if char.isdigit() and char != "0"]
