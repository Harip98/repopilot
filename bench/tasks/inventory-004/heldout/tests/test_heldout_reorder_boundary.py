from inventory.reorder import needs_reorder


def test_reorder_at_the_point():
    assert needs_reorder(6, 2, 3)
    assert needs_reorder(10, 2, 3, safety_days=2)


def test_above_the_point():
    assert not needs_reorder(7, 2, 3)
