"""CLDR-based pluralization. Forms are passed as list from catalog."""
from __future__ import annotations


def plural(n: int, lang: str, forms: list[str]) -> str:
    """Select plural form for *n* in *lang*.

    Rules:
      zh: 1 form (index 0).
      en, de: 2 forms — [singular, plural]. n==1 → 0, else → 1.
      ru: 3 forms — [n%10==1 && n%100!=11, n%10 in 2-4 && n%100 not in 12-14, else].

    On out-of-bounds or empty forms: return str(n).
    """
    if not forms:
        return str(n)

    def _safe(idx: int) -> str:
        if 0 <= idx < len(forms):
            return forms[idx].replace("{n}", str(n))
        return str(n)

    if lang == "zh":
        return _safe(0)

    if lang in ("en", "de"):
        return _safe(0 if n == 1 else 1)

    if lang == "ru":
        n10 = abs(n) % 10
        n100 = abs(n) % 100
        if n10 == 1 and n100 != 11:
            return _safe(0)
        if 2 <= n10 <= 4 and not (12 <= n100 <= 14):
            return _safe(1)
        return _safe(2)

    # Unknown lang: use index 0 (most permissive fallback)
    return _safe(0)
