"""The shared password gate.

Every page needs this, not just the main one. Streamlit's `pages/` are
independently reachable by URL and from the sidebar, so a gate that lives in
`streamlit_app.py` guards `streamlit_app.py` and nothing else - the probe
page was open to anyone with the deployment's address until this was pulled
out here.

`authorised` lives in session state, which is shared across pages, so
answering once covers the whole app for that visitor.
"""

from __future__ import annotations

import hmac

import streamlit as st


def password_gate(title: str = "RoadRunner Agent") -> bool:
    """True when the visitor may see the page.

    With no `APP_PASSWORD` in secrets the app is open. That is the right
    default locally, where requiring setup to run your own code would be
    absurd, and the wrong one on a deployment - which makes forgetting it the
    easy mistake. See the deployment notes in CLAUDE.md.
    """
    try:
        expected = st.secrets.get("APP_PASSWORD", "")
    except Exception:            # no secrets.toml at all
        expected = ""
    if not expected:
        return True
    if st.session_state.get("authorised"):
        return True

    st.title(title)
    st.caption("This deployment is password protected.")
    entered = st.text_input("Password", type="password")
    if entered:
        # compare_digest rather than ==, so a wrong guess takes the same
        # time whatever it got right.
        if hmac.compare_digest(entered, expected):
            st.session_state.authorised = True
            st.rerun()
        else:
            st.error("Not that one.")
    return False
