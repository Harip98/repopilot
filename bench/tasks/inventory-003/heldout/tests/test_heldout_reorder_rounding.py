from inventory.reorder import reorder_point


def test_rounds_up():
    assert reorder_point(2.5, 3) == 8
    assert reorder_point(0.4, 7) == 3
    assert reorder_point(1.25, 2, safety_days=1) == 4


def test_whole_numbers_unchanged():
    assert reorder_point(2, 3) == 6
