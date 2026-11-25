from sqlalchemy import and_, func, or_

# EDGAR form families whose documents are fund administration, structured-note
# terms, asset-backed servicing data, or exempt-offering notices. They arrive
# in the largest volumes from the all-issuer feeds, and each one rarely changes
# an investment view. They stay eligible for extraction; the queue only lets
# substantive work captured shortly after them go first.
ROUTINE_FILING_FORMS = (
    "D",
    "D/A",
    "FWP",
    "10-D",
    "10-D/A",
    "24F-2NT",
    "40-17G",
    "424B2",
    "CERT",
    "EFFECT",
    "MA-I",
    "MA-I/A",
    "S-6",
    "X-17A-5",
    "25-NSE",
)
ROUTINE_FILING_FORM_PREFIXES = ("NPORT-", "N-", "485", "497", "ABS-")


def _form(title):
    """The form type EDGAR puts first in its feed titles ("8-K - Issuer ...")."""
    return func.split_part(title, " ", 1)


def is_edgar_current_report(url, title):
    """Identify current-report entries from EDGAR's own feed titles."""
    return and_(
        url.regexp_match(
            r"^https?://(?:www\.)?sec\.gov/Archives/edgar/data/[0-9]+/[0-9]+/[0-9-]+-index\.html?(?:[?#].*)?$",
            flags="i",
        ),
        _form(title).in_(("8-K", "8-K/A", "6-K", "6-K/A")),
    )


def is_edgar_routine_filing(source_item_type, title):
    """Identify routine filing forms (see ROUTINE_FILING_FORMS) by title."""
    form = _form(title)
    return and_(
        source_item_type == "filing",
        or_(
            form.in_(ROUTINE_FILING_FORMS),
            *(form.startswith(prefix) for prefix in ROUTINE_FILING_FORM_PREFIXES),
        ),
    )
