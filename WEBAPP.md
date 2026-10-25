# WG Finance phone app

A phone-first web app (installable to the home screen) that replaces the
Streamlit dashboard. It uses the same Google Sheet, logins, Groq receipt
reader and money maths as before.

```
vercel.json      Vercel Services config: one public service, "web"
frontend/        the app the phone loads (HTML/CSS/JS), served by the API function
api/index.py     the JSON API (FastAPI) — runs as one Vercel Python function
app/services/    shared code: Google Sheets, logins, ledger maths, Groq parser
```

- **No sleep mode.** The app files are cached by Vercel's edge network and
  by the phone itself, so it opens instantly. The API starts on demand in about a second; there is no
  30-second "waking up" screen.
- **Works like an app.** Bottom tab bar, big buttons, camera capture,
  stays logged in, opens offline with the last loaded data.
- **Fewer Google requests.** One request loads everything a screen needs,
  so the Google Sheets quota is no longer a problem.

## Deploy on Vercel (free)

1. Sign in at <https://vercel.com> with GitHub and click **Add New → Project**.
2. Import `ilohas168/wg-finance`. Set the **Framework Preset** to
   **Services** and leave the root directory as `./`. The repo's
   `vercel.json` defines one service, `web` (the FastAPI app in
   `api/index.py`, which also serves the phone app from `frontend/`), and
   routes all traffic to it.
3. Under **Environment Variables**, add the same secrets the Streamlit app has:

   | Name | Value |
   |---|---|
   | `GOOGLE_SHEET_ID` | your sheet ID |
   | `GOOGLE_CREDENTIALS_JSON_B64` | the base64 service-account JSON (same as in Streamlit secrets) |
   | `GROQ_API_KEY` | your Groq key |
   | `APP_SECRET` | optional — any long random text; signs logins. If absent, one is derived from the secrets above. |

4. Click **Deploy**. You get a URL like `https://wg-finance.vercel.app`.
5. On each phone, open the URL, log in once, then install it:
   - **iPhone (Safari):** Share → *Add to Home Screen*
   - **Android (Chrome):** ⋮ → *Install app*

Every push to `main` redeploys automatically. Pushes to other branches get
their own preview URL, so changes can be tried before merging.

## Run locally

```bash
pip install -r requirements.txt
uvicorn api.index:app --reload --port 8000   # serves the app and the API
```

Open <http://localhost:8000>. Use the same environment variables as above
(e.g. in `.env`, loaded by your shell).

## Retiring Streamlit

The Streamlit dashboard (`dashboard.py`) keeps working on the same sheet, so
both can run side by side. Once everyone uses the phone app, delete the app
on share.streamlit.io and remove `streamlit` and `plotly` from
`requirements.txt` to make the Vercel function smaller and faster to start.
