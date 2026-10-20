import re
import unicodedata

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_len: int | None = None, sep: str = "-") -> str:
    """Lower-case ASCII slug. Accents are transliterated (é -> e), every run of other
    characters becomes one `sep`, and `max_len` keeps whole words only."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = _NON_ALNUM.sub(sep, ascii_text.lower()).strip(sep)
    if max_len is None or len(slug) <= max_len:
        return slug
    kept = ""
    for word in slug.split(sep):
        candidate = word if not kept else f"{kept}{sep}{word}"
        if len(candidate) > max_len:
            break
        kept = candidate
    return kept or slug[:max_len]
