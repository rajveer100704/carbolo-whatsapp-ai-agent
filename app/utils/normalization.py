def normalize_variant(v: str) -> str:
    """
    Standardizes variant names to prevent normalization mismatches.
    Converts to lowercase, removes whitespace, replaces pluses with 'plus',
    strips dashes, and replaces ampersands with 'and'.
    """
    if not v:
        return ""
    return (
        v.lower()
        .replace(" ", "")
        .replace("+", "plus")
        .replace("-", "")
        .replace("&", "and")
    )
