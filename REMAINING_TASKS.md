# WG Finance — Remaining Tasks & Open Issues

## Session Goal: Resolve Streamlit Cloud authentication and complete remaining features.

---

## BLOCKER: GOOGLE_CREDENTIALS_JSON Not Working on Streamlit Cloud

### Current State
- Service account JSON file exists locally: `wg-finance-bot-6112de07abed.json`
- Code in `app/services/ledger.py:_get_gspread_client()` tries 4 credential sources (priority order):
  a. `st.secrets["gcp_service_account"]` (TOML dict)
  b. `GOOGLE_CREDENTIALS_JSON` env var or `st.secrets["GOOGLE_CREDENTIALS_JSON"]` (raw JSON string)
  c. `GOOGLE_CREDENTIALS_FILE` / `st.secrets["GOOGLE_CREDENTIALS_FILE"]` (file path)
  d. Local file fallback

### What We Tried
1. **Double-quoted TOML** `"..."` — FAILS: Streamlit Cloud interprets `\n` as real newlines, breaking JSON parsing. Private key gets corrupted.
2. **Triple-double-quotes** `"""..."""` — Same problem, even worse because ALL `\n` in the file become real newlines.
3. **Triple-single-quotes** `'''...'''` — Streamlit Cloud UI doesn't support this (only standard double-quote TOML).
4. **`st.secrets["gcp_service_account"]` as nested dict** — Would require a local `.streamlit/secrets.toml` which doesn't deploy to Streamlit Cloud.
5. **JSON sanitizer** (`_sanitize_json_for_streamlit_secrets()`) — Tried and removed; there is no reliable way to reconstruct the original JSON from a multi-line TOML value because `\n` inside the private_key gets mangled by TOML's escaping rules.

### Why Streamlit Cloud Fails
Streamlit Cloud uses TOML parsing for the Secrets form. TOML treats `"..."` strings as having escape interpretation — `\n` becomes real newline (0x0A). When a Google service account JSON is pasted into Streamlit Cloud's single-line secret field, the `\n` characters inside the private_key value are converted to real newlines, making it invalid JSON.

### Recommended Approaches (in priority order):

#### Option A: Deploy with local `.streamlit/secrets.toml` for development
- Create `.streamlit/secrets.toml` with `GOOGLE_CREDENTIALS_JSON = '...single-line...'` (already exists at project root)
- **For Streamlit Cloud deployment**: Use the `GCP_SERVICE_ACCOUNT` TOML dict approach instead:

```toml
# In Streamlit Cloud Settings → Secrets, enter as individual key-value pairs under a nested section:
[gcp_service_account]
type = "service_account"
project_id = "wg-finance-bot"
private_key_id = "6112de07abedf2abefcfebb8eea301feeab4b1f9"
private_key = """-----BEGIN PRIVATE KEY-----\nMIIEvQ...-----END PRIVATE KEY-----\n"""
client_email = "wg-finance-bot@wg-finance-bot.iam.gserviceaccount.com"
client_id = "104716336604048724197"
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url = "https://www.googleapis.com/robot/v1/metadata/x509/wg-finance-bot%40wg-finance-bot.iam.gserviceaccount.com"
universe_domain = "googleapis.com"
```

**This is the correct approach for Streamlit Cloud.** The nested TOML dict avoids the `\n` escaping issue because each field is entered separately, and `"""..."""` triple-double-quotes in TOML preserve `\n` literally.

#### Option B: Use `GOOGLE_CREDENTIALS_FILE` to point to a mounted file
- Not applicable for Streamlit Cloud (no file system persistence).

#### Option C: Set secret as raw string with proper escaping
- In Streamlit Cloud, the JSON needs `\n` to remain literal. If pasting works, it must NOT have real newlines anywhere in the value. The JSON on one line is correct format but Streamlit Cloud may still apply TOML escaping even for single-line inputs.

### ACTION REQUIRED: Use the nested `[gcp_service_account]` TOML dict approach above. This is what `_get_gspread_client()` already checks first (item a). Just configure it correctly in the Streamlit Cloud dashboard by adding each field as a separate line under the `[gcp_service_account]` section header.

---

## PENDING FEATURE TASKS

### 1. Telegram Bot: Roommate Names
**File:** `app/main.py` (already updated partially)
- **DONE:** ROOMMATE_MAP changed to Shin/Fabian/Pierre, _DEFAULT_PAYER = "Shin"
- **TODO:** Verify the `_format_receipt_reply()` function displays names correctly

### 2. Auth Tests
**No auth tests exist yet.** Add unit tests:
- `tests/test_auth.py` — Test `_ensure_users_sheet()`, `verify_user()`, `change_password()`
- Need mock for Google Sheets client (use `unittest.mock.patch` or `gspread` mock)

### 3. Update app/dashboard.py compute_current_balances() stub
**File:** `app/dashboard.py:109-129`
- Still uses legacy Summary Ledger sheet approach, returns zeros
- Should query the new Receipts + Receipt_Items sheets (like the main dashboard does)

### 4. App Config Module (optional)
The task mentioned creating `app/config.py` but it doesn't exist and isn't needed — all config resolution happens in:
- `app/services/ledger.py:_get_sheet_id()` — sheet ID resolution
- `app/services/ledger.py:_get_gspread_client()` — credential resolution
- `app/dashboard.py:SHEET_ID` — legacy dashboard (still uses the same pattern)
- `app/services/auth.py` — auth config

### 5. HANDOFF.md Update
**File:** `HANDOFF.md` — needs new section documenting:
- GOOGLE_CREDENTIALS_JSON Streamlit Cloud fix attempt
- Current blocker with TOML `\n` escaping

---

## WHAT WORKS NOW (after commits up to 814e59c)

| Feature | Status |
|---------|--------|
| CHF currency display | ✅ All $ replaced with CHF across dashboard.py, app/dashboard.py, app/main.py |
| Grand total validation | ✅ Tab 1 shows comparison between line items sum and receipt total |
| Named roommates (Shin/Fabian/Pierre) | ✅ Updated everywhere: models.py, dashboard.py, app/dashboard.py, app/main.py, tests/ |
| SplitType enum extension | ✅ Added ONLY_SHIN, ONLY_FABI, ONLY_PIERRE, SPLIT_SF, SPLIT_SP, SPLIT_FP |
| Beneficiary display labels | ✅ _BENEFICIARY_LABELS maps codes to names; SelectboxColumn uses display names |
| Login UI (sidebar) | ✅ Full login form with logout, change password expander, guest mode |
| Payer auto-assignment | ✅ Logged-in user name is auto-set as Paid_By on save |
| Guest mode | ✅ Tab 1 hidden when not logged in; "View History" shown instead |
| Password hashing | ✅ werkzeug.generate_password_hash + check_password_hash |
| _ensure_users_sheet() | ✅ Lazy seeding with changeme default passwords, lowercase usernames |
| Error distinction (DB vs creds) | ✅ verify_user returns (RESULT_DB_ERROR, ...) for connection failures vs (RESULT_BAD_CREDS, ...) for bad passwords |
| JSON loading priority chain | ✅ 4 sources: dict → env JSON → env file → local fallback |
| Test suite | ✅ All 65 tests pass |

---

## CRITICAL: The GOOGLE_CREDENTIALS_JSON Problem — Deep Explanation

### Root Cause
Google service account JSON contains a `private_key` field with the value:
```
-----BEGIN PRIVATE KEY-----\nMIIEvQIB...\n-----END PRIVATE KEY-----\n
```
The `\n` here are **literal backslash-n** (two characters) — NOT real newlines. This is how Google exports the key and how `json.dumps()` serializes it (with separators that remove whitespace).

### The Problem Chain
1. Streamlit Cloud Settings → Secrets form stores values as TOML
2. TOML `"..."` strings interpret `\n` → real newline character (0x0A)
3. Python's `json.loads()` rejects real newlines inside JSON string values even with `strict=False`
4. Google auth library needs the private_key to have real newlines between PEM parts, but the intermediate conversion destroys the structure

### Correct Solution for Streamlit Cloud: Use `[gcp_service_account]` nested TOML dict
```toml
[gcp_service_account]
type = "service_account"
project_id = "wg-finance-bot"
private_key_id = "6112de07abedf2abefcfebb8eea301feeab4b1f9"
private_key = """-----BEGIN PRIVATE KEY-----\nMIIEvQ...\n-----END PRIVATE KEY-----\n"""
client_email = "wg-finance-bot@wg-finance-bot.iam.gserviceaccount.com"
client_id = "104716336604048724197"
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url = "https://www.googleapis.com/robot/v1/metadata/x509/wg-finance-bot%40wg-finance-bot.iam.gserviceaccount.com"
universe_domain = "googleapis.com"
```

**Why this works:** `"""..."""` in TOML (literal string) preserves `\n` as literal backslash-n, so when Python's json parser reads the resulting dict, the private_key contains `\n` which then gets unescaped to real newlines by `_unescape_private_key()`, and Google's auth library parses the PEM correctly.

**Alternative in Streamlit Cloud:** If you can enter `private_key` as a multi-line value (with actual newlines between each base64 line of the key), that also works because TOML triple-quotes preserve literal newlines inside them.

---

## NEXT SESSION ACTION ITEMS

1. **Fix Streamlit Cloud auth** — Set `gcp_service_account` as nested TOML dict in Streamlit Cloud Settings (see Option A above)
2. **Test login flow** end-to-end on deployed app
3. **Add tests for auth module** (`test_auth.py`)
4. **Update legacy dashboard stub** in `app/dashboard.py:compute_current_balances()` to use new sheets
5. **Push and verify Streamlit Cloud deploy**
