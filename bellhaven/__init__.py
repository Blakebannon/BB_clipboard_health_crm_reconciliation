"""Bellhaven Senior Living <-> Clipboard Health CRM reconciliation toolkit.

Read-only by default. The only code path that mutates the CRM is
``bellhaven.apply.apply_proposal`` and it can only be reached through an
explicit reviewer approval in the Streamlit review app.
"""

__version__ = "1.0.0"
