from textkit import slugify


def test_accents_are_transliterated():
    assert slugify("Crème brûlée") == "creme-brulee"
    assert slugify("Ångström") == "angstrom"
    assert slugify("naïve café") == "naive-cafe"
