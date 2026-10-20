from inventory.reorder import needs_reorder, reorder_point


def test_reorder_point_whole_units():
    assert reorder_point(2, 3) == 6
    assert reorder_point(2, 3, safety_days=2) == 10


def test_needs_reorder():
    assert needs_reorder(4, 2, 3)
    assert not needs_reorder(9, 2, 3)
