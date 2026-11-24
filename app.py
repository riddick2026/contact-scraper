"""Streamlit front-end for the contact-info scraper.

Run with:  streamlit run app.py
"""

import db
import streamlit as st

environment = db.app_environment()
is_test = environment != "production"

st.set_page_config(page_title="Contact Scraper" + (f" ({environment.upper()})" if is_test else ""),
                   page_icon="🧪" if is_test else "🔎", layout="wide")

if is_test:
    st.markdown(
        f"""<div style="background:#c2410c;color:#fff;padding:.55rem 1rem;border-radius:.5rem;
        font-weight:700;letter-spacing:.03em;margin-bottom:.75rem">
        🧪 {environment.upper()} ENVIRONMENT · changes here don't affect production data</div>""",
        unsafe_allow_html=True,
    )

st.navigation([
    st.Page("scan_page.py", title="Scan", icon="🛰️", default=True),
    st.Page("viewer_page.py", title="Database viewer", icon="🗂️"),
]).run()
