"""Streamlit front-end for the contact-info scraper.

Run with:  streamlit run app.py
"""

import streamlit as st

st.set_page_config(page_title="Contact Scraper", page_icon="🔎", layout="wide")

st.navigation([
    st.Page("scraper_page.py", title="Scraper", icon="🔎", default=True),
    st.Page("viewer_page.py", title="Database viewer", icon="🗂️"),
]).run()
