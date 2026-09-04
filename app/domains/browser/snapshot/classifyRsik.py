"""
==========================================================
Risk Classifier
==========================================================

Responsibility:
Assign a risk category to an interactive element based on
its accessible label.

This is used by snapshotBuilder.py when constructing the
Browser Snapshot that will be sent to the LLM.
==========================================================
"""


def classify_risk(label: str = "") -> str:
    """
    Classify an element into a predefined risk category.

    Parameters
    ----------
    label : str
        Accessible label extracted from the ARIA snapshot.

    Returns
    -------
    str
        Risk category.
    """

    text = label.lower()

    # ------------------------------------------------------
    # Destructive Actions
    # ------------------------------------------------------

    if any(word in text for word in [
        "delete",
        "remove",
        "hapus"
    ]):
        return "destructive_action"

    # ------------------------------------------------------
    # External Messaging
    # ------------------------------------------------------

    if any(word in text for word in [
        "send",
        "message",
        "kirim"
    ]):
        return "external_message"

    # ------------------------------------------------------
    # Financial Actions
    # ------------------------------------------------------

    if any(word in text for word in [
        "pay",
        "checkout",
        "purchase",
        "bayar"
    ]):
        return "financial_action"

    # ------------------------------------------------------
    # Data Export
    # ------------------------------------------------------

    if "download" in text:
        return "data_export"

    # ------------------------------------------------------
    # Default
    # ------------------------------------------------------

    return "low_risk"