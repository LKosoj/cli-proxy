"""Human-readable language names for agent instructions (English form)."""

LANGUAGE_NAMES: dict[str, str] = {
    "ru": "Russian",
    "en": "English",
    "zh": "Chinese",
    "de": "German",
}


def get_language_name(lang: str, fallback: str = "Russian") -> str:
    """Return English name of *lang* for use in agent prompts."""
    return LANGUAGE_NAMES.get(lang, fallback)
