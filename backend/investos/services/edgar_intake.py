from sqlalchemy import and_, func


def is_edgar_current_report(url, title):
    """Identify current-report entries from EDGAR's own feed titles."""
    return and_(
        url.regexp_match(
            r"^https?://(?:www\.)?sec\.gov/Archives/edgar/data/[0-9]+/[0-9]+/[0-9-]+-index\.html?(?:[?#].*)?$",
            flags="i",
        ),
        func.split_part(title, " ", 1).in_(("8-K", "8-K/A", "6-K", "6-K/A")),
    )
