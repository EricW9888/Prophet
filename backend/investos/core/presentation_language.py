"""Presentation preferences never change schema keys or evidence contracts."""

from typing import Literal

PresentationLanguage = Literal["en", "es", "fr", "zh-Hans"]
LANGUAGES = {"en": "English", "es": "Español", "fr": "Français", "zh-Hans": "简体中文"}


def presentation_instruction(*fields: str) -> str:
    from investos.services.runtime_settings import RuntimeSettingsStore

    language = RuntimeSettingsStore.load().presentation_language
    return (
        f"\nPRESENTATION: Write only these human-facing fields in {LANGUAGES[language]} "
        f"({language}): {', '.join(fields)}. Preserve exact identifiers, citations, "
        "dates, numbers and financial units. Keep schema keys, enums, subject names, "
        "evidence quotations, tool arguments, search queries and machine-readable "
        "assertions unchanged. This preference is not evidence or a change in conclusions."
    )
