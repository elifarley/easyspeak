"""Tests for the gridydotool plugin: the zone math, its parity with mousegrid,
and the parser that turns an utterance into zone numbers."""

from easyspeak.plugins import gridydotool, mousegrid


class TestZoneMath:
    def test_subdivide_each_zone(self):
        bounds = (0, 0, 900, 600)
        expected = {
            1: (0, 0, 300, 200),
            2: (300, 0, 300, 200),
            3: (600, 0, 300, 200),
            4: (0, 200, 300, 200),
            5: (300, 200, 300, 200),
            6: (600, 200, 300, 200),
            7: (0, 400, 300, 200),
            8: (300, 400, 300, 200),
            9: (600, 400, 300, 200),
        }
        for zone, want in expected.items():
            assert gridydotool.subdivide(bounds, zone) == want

    def test_subdivide_truncates_like_the_extension_backend(self):
        # 1000 // 3 == 333, and the same recursion must produce the same cell on
        # both backends or the parity invariant is gone.
        assert gridydotool.subdivide((0, 0, 1000, 1000), 9) == (666, 666, 333, 333)

    def test_center(self):
        assert gridydotool.center((1351, 280, 71, 40)) == (1386, 300)


class TestContainmentInvariant:
    def test_bounds_never_leave_the_monitor(self):
        """x + w and y + h stay inside the monitor for every sequence."""
        monitor = (1920, 1080)
        for a in range(1, 10):
            for b in range(1, 10):
                for c in range(1, 10):
                    bounds = (0, 0, monitor[0], monitor[1])
                    for zone in (a, b, c):
                        bounds = gridydotool.subdivide(bounds, zone)
                    x, y, w, h = bounds
                    assert x + w <= monitor[0], (a, b, c)
                    assert y + h <= monitor[1], (a, b, c)


class TestParityWithMousegrid:
    """The gate shadows mousegrid's words, so both must land on the same pixel.

    Driven against mousegrid's own implementation, which is the only
    independent oracle available for this arithmetic.
    """

    @staticmethod
    def _mousegrid_bounds(sequence, monitor, monkeypatch):
        monkeypatch.setattr(mousegrid, "dbus_call", lambda *args: True)
        monkeypatch.setattr(mousegrid, "screen_size", monitor)
        monkeypatch.setattr(mousegrid, "grid_active", True)
        monkeypatch.setattr(mousegrid, "grid_bounds", (0, 0, monitor[0], monitor[1]))
        for zone in sequence:
            mousegrid.update_grid(zone)
        return mousegrid.grid_bounds

    def test_all_sequences_agree(self, monkeypatch):
        monitor = (1920, 1080)
        for a in range(1, 10):
            for b in range(1, 10):
                for c in range(1, 10):
                    theirs = self._mousegrid_bounds((a, b, c), monitor, monkeypatch)
                    bounds = (0, 0, monitor[0], monitor[1])
                    for zone in (a, b, c):
                        bounds = gridydotool.subdivide(bounds, zone)
                    assert bounds == theirs, (a, b, c)
                    assert gridydotool.center(bounds) == mousegrid.get_center()

    def test_trigger_words_equal(self):
        assert gridydotool.GRID_TRIGGERS == mousegrid.GRID_TRIGGERS

    def test_number_words_equal(self):
        assert gridydotool.WORD_TO_NUM == mousegrid.WORD_TO_NUM

    def test_parser_agrees_on_a_sample(self):
        for text in ("3 7 5", "three seven five", "tree ate nein", "one two"):
            assert gridydotool.parse_number_sequence(text) == (
                mousegrid.parse_number_sequence(text)
            )
