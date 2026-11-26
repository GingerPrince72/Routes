# Routes 9.5

Streamlit delivery-route planner. The application source and tests are here; private outlet databases, request journals, and API keys are intentionally excluded.

From this directory, run the app with Python and the packages in requirements.txt:

    python -m streamlit run app.py --server.address 0.0.0.0 --server.port 8520

The Google Maps API key is supplied through the GOOGLE_MAPS_API_KEY environment variable. Google Places searches and route calculations may incur charges.

Use ROUTES_DB_PATH for the existing private outlet database and ROUTES_REQUEST_JOURNAL_PATH for its existing request journal. Keep and reuse the existing journal: an empty journal does not contain earlier request history. Restore private data securely rather than committing it to GitHub.
