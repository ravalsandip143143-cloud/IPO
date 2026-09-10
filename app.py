"""
==========================================================================
IPO TRACKER TOOL  —  by S.K. (with Claude)
==========================================================================
Ek hi file mein sab kuch:
 1) IPO list scraping (Chittorgarh / InvestorGain se)
 2) Local storage (CSV files data/ folder mein — GitHub repo hi database hai)
 3) Sector-wise scoring + color coding
 4) Angel One SmartAPI login (auto session) + live/listing price fetch
 5) Telegram notifications (naya IPO + listing update)
 6) Streamlit UI — list, click-to-detail, delete button, Excel download

STORAGE LOGIC (P6):
 - Hum GitHub repo ko hi database ki tarah use kar rahe hain.
 - Sab data data/ folder ke andar CSV files mein save hota hai.
 - GitHub Actions cron job roz ye script chalayega aur naye data ko
   "git commit + push" kar dega — isse tumhara data GitHub par hi safe
   rehta hai (free, unlimited practically for this scale).
 - Streamlit Cloud khud data ko permanently store nahi karta (restart
   pe delete ho sakta hai), isliye asli data hamesha GitHub repo se
   hi load/save hoga.
==========================================================================
"""

import os
import json
import re
import time
import datetime as dt
from io import BytesIO

import requests
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup

# Angel One + TOTP (login ke liye) -- abhi optional rakha hai
try:
    from SmartApi import SmartConnect
    import pyotp
    ANGEL_AVAILABLE = True
except Exception:
    ANGEL_AVAILABLE = False

# NSE official (unofficial wrapper) -- IPO list ke liye reliable source.
# InvestorGain/Chittorgarh ka data JavaScript se load hota hai, isliye
# simple requests se nahi milta -- ye package cookie/session khud handle
# karke seedha JSON data deta hai NSE se.
try:
    from nse import NSE as NSEClient
    NSE_AVAILABLE = True
except Exception:
    NSE_AVAILABLE = False

# ==========================================================================
# 0. CONSTANTS / PATHS
# ==========================================================================

DATA_DIR = "data"
IPO_MASTER_FILE = os.path.join(DATA_DIR, "ipo_master.csv")      # sabhi IPOs ka master record
GMP_HISTORY_FILE = os.path.join(DATA_DIR, "gmp_history.csv")    # day-wise GMP/subscription
DELETED_FILE = os.path.join(DATA_DIR, "deleted_ipos.csv")       # manually hidden IPOs

os.makedirs(DATA_DIR, exist_ok=True)

# Sector-wise "normal / healthy" benchmark ranges (P8)
# Ye tum time ke saath tune kar sakte ho jaise-jaise experience badhega.
SECTOR_BENCHMARKS = {
    "IT/Cloud/Tech":       {"pe_max": 45, "pb_max": 10, "de_max": 0.5},
    "Pharma/API":          {"pe_max": 40, "pb_max": 8,  "de_max": 0.8},
    "EPC/Infra/Power":     {"pe_max": 25, "pb_max": 4,  "de_max": 2.0},
    "Manufacturing/Engg":  {"pe_max": 30, "pb_max": 5,  "de_max": 1.0},
    "Jewellery/Retail":    {"pe_max": 30, "pb_max": 6,  "de_max": 1.2},
    "Logistics/Services":  {"pe_max": 30, "pb_max": 5,  "de_max": 1.5},
    "Fashion/Lifestyle":   {"pe_max": 35, "pb_max": 6,  "de_max": 1.0},
    "Default":             {"pe_max": 30, "pb_max": 6,  "de_max": 1.0},
}

# Lock-in period defaults (P3) — SEBI standard norms (days)
LOCKIN_ANCHOR_DAYS = 90        # anchor investor lock-in (approx, mainboard)
LOCKIN_PROMOTER_MIN_DAYS = 545  # ~18 months minimum promoter lock-in (mainboard, approx)
LOCKIN_SME_PROMOTER_DAYS = 1095  # SME promoter lock-in often 3 years for part of holding

# ==========================================================================
# 1. STORAGE HELPERS  (P6 — GitHub repo / CSV based "database")
# ==========================================================================

def load_csv(path, columns):
    """CSV file load karo, agar exist nahi karti to empty dataframe banao."""
    if os.path.exists(path):
        try:
            return pd.read_csv(path)
        except Exception:
            return pd.DataFrame(columns=columns)
    return pd.DataFrame(columns=columns)


def save_csv(df, path):
    df.to_csv(path, index=False)


MASTER_COLUMNS = [
    "company_name", "board_type", "nse_sme_listed", "status",
    "open_date", "close_date", "listing_date",
    "issue_price_low", "issue_price_high",
    "issue_pe", "issue_pb", "issue_debt_equity", "issue_ebitda_margin",
    "issue_debt", "issue_book_value", "issue_market_cap",
    "sector",
    "listing_price", "current_price", "current_pb", "current_debt_equity",
    "current_market_cap", "last_updated",
    "score", "score_color",
    "anchor_lockin_expiry", "promoter_lockin_expiry",
    "rhp_link", "added_on",
]

GMP_COLUMNS = ["company_name", "date", "gmp", "subscription_qib",
               "subscription_nii", "subscription_retail", "subscription_total"]


def load_master():
    return load_csv(IPO_MASTER_FILE, MASTER_COLUMNS)


def load_gmp_history():
    return load_csv(GMP_HISTORY_FILE, GMP_COLUMNS)


def load_deleted():
    return load_csv(DELETED_FILE, ["company_name"])


# ==========================================================================
# 2. SCRAPER  (P1, P2, P5)
# ==========================================================================
# NOTE: Website ka HTML structure kabhi bhi badal sakta hai — agar scraper
# fail ho to sabse pehle yahan CSS selectors check karna. Maine yahan
# InvestorGain ka IPO GMP page use kiya hai (public, free).

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# NSE package apna cookie file yahan store karega
NSE_DOWNLOAD_DIR = os.path.join(DATA_DIR, "nse_session")
os.makedirs(NSE_DOWNLOAD_DIR, exist_ok=True)

# NSE SME symbol list ek baar cache kar lete hain (baar baar call na karna pade)
_nse_sme_symbols_cache = None


def _get_nse_sme_symbols():
    """NSE par listed sabhi SME symbols ka set laata hai (cached)."""
    global _nse_sme_symbols_cache
    if _nse_sme_symbols_cache is not None:
        return _nse_sme_symbols_cache
    if not NSE_AVAILABLE:
        return set()
    try:
        with NSEClient(NSE_DOWNLOAD_DIR, server=True) as nse:
            data = nse.listSme()
        symbols = {item.get("symbol", "").upper() for item in data.get("data", [])}
        _nse_sme_symbols_cache = symbols
        return symbols
    except Exception:
        return set()


def scrape_ipo_list():
    """
    NSE ke official (unofficial wrapper) package se current + upcoming
    IPO list laata hai. Ye JSON-based hai, isliye InvestorGain/Chittorgarh
    jaisi JS-rendering wali dikkat nahi aati.
    """
    ipos = []
    if not NSE_AVAILABLE:
        st.warning("`nse` package install nahi hai. requirements.txt check karo.")
        return ipos

    try:
        with NSEClient(NSE_DOWNLOAD_DIR, server=True) as nse_client:
            current = nse_client.listCurrentIPO() or []
            upcoming = nse_client.listUpcomingIPO() or []

        sme_symbols = _get_nse_sme_symbols()

        for item in current + upcoming:
            name = item.get("companyName") or item.get("symbol") or ""
            if not name:
                continue
            symbol = (item.get("symbol") or "").upper()
            is_sme = symbol in sme_symbols
            ipos.append({
                "company_name": name,
                "board_type": "SME" if is_sme else "Mainboard",
                "open_date": item.get("issueStartDate"),
                "close_date": item.get("issueEndDate"),
                "issue_price_raw": item.get("issuePrice"),
                "subscription_total": item.get("noOfTime"),  # NSE ka subscription multiple
                "status": item.get("status"),
            })
    except Exception as e:
        st.warning(f"IPO list fetch mein dikkat aayi: {e}")
    return ipos


def _company_to_ipoji_slug(company_name):
    """
    Company name ko ipoji.com URL slug mein convert karta hai.
    Example: "Veegaland Developers Limited" -> "veegaland-developers-ipo"
    NOTE: Ye ek best-effort guess hai -- kuch companies ka slug thoda
    alag ho sakta hai (jaise short names use karte hain). Agar match
    na mile to ye function None return karega, aur wo company ke liye
    GMP/fundamentals khaali reh jaayenge -- manually bhi daal sakte ho.
    """
    name = company_name
    for suffix in [" Limited", " Ltd.", " Ltd", " (India)", " India Limited"]:
        name = name.replace(suffix, "")
    slug = re.sub(r"[^a-zA-Z0-9\s-]", "", name).strip().lower()
    slug = re.sub(r"\s+", "-", slug)
    return f"{slug}-ipo"


def fetch_ipoji_details(company_name):
    """
    ipoji.com se ek company ka GMP + fundamentals (P/E, P/B, Debt/Equity,
    ROE, PAT Margin, Market Cap) nikalta hai. Ye site static HTML deti
    hai (InvestorGain/Chittorgarh ke ulat), isliye simple requests se
    kaam ho jaata hai.
    """
    result = {
        "gmp": None, "gmp_percent": None, "subscription_total": None,
        "pe": None, "pb": None, "debt_equity": None,
        "pat_margin": None, "market_cap": None,
    }
    slug = _company_to_ipoji_slug(company_name)
    if not slug:
        return result

    url = f"https://www.ipoji.com/ipo/{slug}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return result
        text = BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True)

        # GMP: "IPO GMP today is ₹18 per share (a 13% premium over..."
        gmp_match = re.search(r"GMP today is\s*₹?\s*([\d.]+)\s*per share.*?(\d+)%\s*premium", text)
        if gmp_match:
            result["gmp"] = float(gmp_match.group(1))
            result["gmp_percent"] = float(gmp_match.group(2))

        # Valuation snapshot sentence: "P/E 25.64, EPS ₹5.46/-, P/B 1.77,
        # RoNW 16.02%, and market cap ₹682.50 Cr."
        val_match = re.search(
            r"valuation snapshot:\s*P/E\s*([\d.]+|N/A),.*?P/B\s*([\d.]+|N/A),\s*RoNW\s*([\d.]+|N/A)%.*?market cap\s*₹?\s*([\d,.]+|N/A)\s*Cr",
            text
        )
        if val_match:
            pe, pb, ronw, mcap = val_match.groups()
            result["pe"] = float(pe) if pe != "N/A" else None
            result["pb"] = float(pb) if pb != "N/A" else None
            result["pat_margin"] = float(ronw) if ronw != "N/A" else None  # RoNW proxy
            result["market_cap"] = float(mcap.replace(",", "")) if mcap != "N/A" else None

        # Debt / Equity -- appears as "Debt / Equity (...) 0.32"
        de_match = re.search(r"Debt\s*/\s*Equity[^0-9]*?([\d.]+)", text)
        if de_match:
            result["debt_equity"] = float(de_match.group(1))

        # PAT Margin -- "PAT Margin (...) 10.47%"
        pat_match = re.search(r"PAT Margin[^0-9]*?([\d.]+)%", text)
        if pat_match:
            result["pat_margin"] = float(pat_match.group(1))

        # Subscription total -- "Total 0.02x"
        sub_match = re.search(r"Total\s*([\d.]+)x", text)
        if sub_match:
            result["subscription_total"] = float(sub_match.group(1))

    except Exception:
        pass
    return result


def scrape_gmp_and_subscription(company_name):
    """ipoji.com se GMP + subscription nikalta hai (fundamentals alag se fetch_ipoji_details mein)."""
    data = fetch_ipoji_details(company_name)
    return {"gmp": data["gmp"], "subscription_total": data["subscription_total"]}


def is_nse_sme_listed(company_name):
    """
    P5 -- SME IPO ka NSE-SME (NSE Emerge) listing check.
    Ab real NSE data se check hota hai (scrape_ipo_list mein already
    is_sme calculate ho chuka hota hai per-company) -- ye function sirf
    fallback/manual check ke liye rakha hai.
    """
    sme_symbols = _get_nse_sme_symbols()
    return company_name.upper() in sme_symbols


# ==========================================================================
# 3. ANGEL ONE INTEGRATION  (P3, P10 — auto daily login)
# ==========================================================================

def angel_login():
    """
    Angel One SmartAPI mein daily auto-login karta hai.
    Secrets Streamlit ke secrets.toml se aayenge (P — token/API safe rakhna).
    """
    if not ANGEL_AVAILABLE:
        st.error("SmartApi / pyotp install nahi hai. requirements.txt check karo.")
        return None
    try:
        api_key = st.secrets["angel"]["api_key"]
        client_id = st.secrets["angel"]["client_id"]
        password = st.secrets["angel"]["password"]
        totp_secret = st.secrets["angel"]["totp_secret"]

        totp = pyotp.TOTP(totp_secret).now()
        obj = SmartConnect(api_key=api_key)
        session = obj.generateSession(client_id, password, totp)
        if session.get("status"):
            return obj
        else:
            st.error(f"Angel One login fail: {session}")
            return None
    except Exception as e:
        st.error(f"Angel One login error: {e}")
        return None


def get_live_price(obj, symbol_token, exchange="NSE"):
    """Angel One se current market price nikalta hai (symbol_token chahiye)."""
    if obj is None:
        return None
    try:
        data = obj.ltpData(exchange, symbol_token, symbol_token)
        return data["data"]["ltp"]
    except Exception:
        return None


# ==========================================================================
# 4. SCORING LOGIC  (P8 — sector-wise, color coded)
# ==========================================================================

def get_benchmark(sector):
    return SECTOR_BENCHMARKS.get(sector, SECTOR_BENCHMARKS["Default"])


def calculate_score(row):
    """
    Har IPO ka score 0-100 ke beech nikalta hai, based on:
    - PE valuation (sector benchmark ke against)
    - PB valuation
    - Debt/Equity
    - EBITDA margin
    Weight simple rakha hai — tum baad mein tune kar sakte ho.
    """
    bench = get_benchmark(row.get("sector", "Default"))
    score = 0
    max_score = 100

    # PE score (30 points) — jitna sasta (bench se kam), utna zyada score
    pe = row.get("issue_pe")
    if pd.notna(pe) and pe not in (None, "", 0):
        pe = float(pe)
        pe_ratio = bench["pe_max"] / pe if pe > 0 else 0
        score += min(30, 30 * min(pe_ratio, 1.5) / 1.5)

    # PB score (25 points)
    pb = row.get("issue_pb")
    if pd.notna(pb) and pb not in (None, "", 0):
        pb = float(pb)
        pb_ratio = bench["pb_max"] / pb if pb > 0 else 0
        score += min(25, 25 * min(pb_ratio, 1.5) / 1.5)

    # Debt/Equity score (25 points) — kam debt = zyada score
    de = row.get("issue_debt_equity")
    if pd.notna(de) and de not in (None, ""):
        de = float(de)
        de_ratio = bench["de_max"] / de if de > 0 else 1.5
        score += min(25, 25 * min(de_ratio, 1.5) / 1.5)
    else:
        score += 15  # neutral agar data nahi mila

    # EBITDA margin score (20 points) — jitna zyada margin utna better
    margin = row.get("issue_ebitda_margin")
    if pd.notna(margin) and margin not in (None, ""):
        margin = float(margin)
        score += min(20, (margin / 40) * 20)  # 40%+ margin = full marks
    else:
        score += 10

    return round(min(score, max_score), 1)


def score_to_color(score):
    """P8 ka color coding rule."""
    if score >= 80:
        return "#8fd19e"   # strong green
    elif score >= 65:
        return "#c9e8b5"   # light green
    elif score < 40:
        return "#f5b7b1"   # light red
    else:
        return "#e0e0e0"   # light grey (normal)


# ==========================================================================
# 5. TELEGRAM NOTIFICATIONS  (P7)
# ==========================================================================

def send_telegram_message(message):
    """
    Telegram bot se message bhejta hai. Tumhara existing bot token
    reuse hoga — bas naya chat/message bhej denge, alag bot ki zaroorat
    nahi hai.
    """
    try:
        bot_token = st.secrets["telegram"]["bot_token"]
        chat_id = st.secrets["telegram"]["chat_id"]
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        st.warning(f"Telegram message nahi bhej paya: {e}")


def notify_new_ipo(row):
    msg = (
        f"*IPO* 🆕 Naya IPO Aaya!\n\n"
        f"Company: {row['company_name']}\n"
        f"Board: {row['board_type']}\n"
        f"Issue Price: ₹{row.get('issue_price_low','-')} - ₹{row.get('issue_price_high','-')}\n"
        f"PE: {row.get('issue_pe','-')} | PB: {row.get('issue_pb','-')}\n"
        f"Debt/Equity: {row.get('issue_debt_equity','-')}\n"
        f"Score: {row.get('score','-')}/100"
    )
    send_telegram_message(msg)


def notify_listing_update(row):
    msg = (
        f"*IPO* 📈 Listing Update!\n\n"
        f"Company: {row['company_name']}\n"
        f"Issue Price: ₹{row.get('issue_price_low','-')}\n"
        f"Listing Price: ₹{row.get('listing_price','-')}\n"
        f"Current Price: ₹{row.get('current_price','-')}\n"
        f"PB: {row.get('current_pb','-')} | D/E: {row.get('current_debt_equity','-')}"
    )
    send_telegram_message(msg)


# ==========================================================================
# 6. CORE UPDATE PIPELINE (ye function GitHub Actions cron se bhi chalegi)
# ==========================================================================

def run_daily_update():
    """
    Roz ka pura pipeline:
    1) Naye IPOs detect karo -> master mein add karo -> Telegram alert
    2) Open IPOs ka GMP/subscription history update karo
    3) Listed IPOs (1 saal tak) ka current price/fundamentals refresh
    """
    master = load_master()
    deleted = load_deleted()
    gmp_hist = load_gmp_history()

    scraped = scrape_ipo_list()
    today = dt.date.today().isoformat()

    for item in scraped:
        name = item["company_name"]
        if name in deleted["company_name"].values:
            continue  # manually hidden (P4)

        if name not in master["company_name"].values:
            # naya IPO mila -> master mein add karo
            new_row = {c: None for c in MASTER_COLUMNS}

            # Issue price "Rs.200 to Rs.210" jaisa format hota hai NSE se,
            # ise low/high mein split karte hain
            price_low, price_high = None, None
            raw_price = item.get("issue_price_raw")
            if raw_price:
                nums = [p.strip().replace("Rs.", "").replace(",", "")
                        for p in raw_price.replace("Rs.", "").split("to")]
                try:
                    if len(nums) == 2:
                        price_low, price_high = float(nums[0]), float(nums[1])
                    elif len(nums) == 1:
                        price_low = price_high = float(nums[0])
                except ValueError:
                    pass

            new_row.update({
                "company_name": name,
                "board_type": item["board_type"],
                "nse_sme_listed": item["board_type"] == "SME",  # NSE data se hi aaya hai
                "status": item.get("status") or "open",
                "open_date": item.get("open_date"),
                "close_date": item.get("close_date"),
                "issue_price_low": price_low,
                "issue_price_high": price_high,
                "added_on": today,
            })
            new_row["score"] = calculate_score(new_row)
            new_row["score_color"] = score_to_color(new_row["score"])
            master = pd.concat([master, pd.DataFrame([new_row])], ignore_index=True)
            notify_new_ipo(new_row)

        # ipoji.com se GMP + fundamentals dono fetch karo (P2 + scoring data)
        ipoji_data = fetch_ipoji_details(name)

        # GMP history row add karo (P2)
        gmp_row = {
            "company_name": name, "date": today,
            "gmp": ipoji_data.get("gmp"),
            "subscription_qib": None, "subscription_nii": None,
            "subscription_retail": None,
            "subscription_total": ipoji_data.get("subscription_total"),
        }
        gmp_hist = pd.concat([gmp_hist, pd.DataFrame([gmp_row])], ignore_index=True)

        # Master row mein fundamentals update karo (naya ho ya purana, dono ke liye)
        row_idx = master.index[master["company_name"] == name]
        if len(row_idx) > 0:
            idx = row_idx[0]
            if ipoji_data.get("pe") is not None:
                master.at[idx, "issue_pe"] = ipoji_data["pe"]
            if ipoji_data.get("pb") is not None:
                master.at[idx, "issue_pb"] = ipoji_data["pb"]
            if ipoji_data.get("debt_equity") is not None:
                master.at[idx, "issue_debt_equity"] = ipoji_data["debt_equity"]
            if ipoji_data.get("pat_margin") is not None:
                master.at[idx, "issue_ebitda_margin"] = ipoji_data["pat_margin"]
            if ipoji_data.get("market_cap") is not None:
                master.at[idx, "issue_market_cap"] = ipoji_data["market_cap"]
            master.at[idx, "last_updated"] = today

            # Score dobara calculate karo naye data ke saath
            updated_row = master.loc[idx].to_dict()
            new_score = calculate_score(updated_row)
            master.at[idx, "score"] = new_score
            master.at[idx, "score_color"] = score_to_color(new_score)

    save_csv(master, IPO_MASTER_FILE)
    save_csv(gmp_hist, GMP_HISTORY_FILE)
    return master


# ==========================================================================
# 7. EXCEL EXPORT  (P9)
# ==========================================================================

def export_to_excel(df):
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="IPO Tracker")
        workbook = writer.book
        worksheet = writer.sheets["IPO Tracker"]

        # Simple conditional-style coloring based on score column
        from openpyxl.styles import PatternFill
        if "score" in df.columns:
            score_col_idx = df.columns.get_loc("score") + 1
            for row_idx, score in enumerate(df["score"], start=2):
                try:
                    score = float(score)
                except (TypeError, ValueError):
                    continue
                if score >= 80:
                    fill = PatternFill(start_color="8fd19e", end_color="8fd19e", fill_type="solid")
                elif score >= 65:
                    fill = PatternFill(start_color="c9e8b5", end_color="c9e8b5", fill_type="solid")
                elif score < 40:
                    fill = PatternFill(start_color="f5b7b1", end_color="f5b7b1", fill_type="solid")
                else:
                    fill = PatternFill(start_color="e0e0e0", end_color="e0e0e0", fill_type="solid")
                worksheet.cell(row=row_idx, column=score_col_idx).fill = fill
    return output.getvalue()


# ==========================================================================
# 8. STREAMLIT UI
# ==========================================================================

st.set_page_config(page_title="IPO Tracker Tool", layout="wide")
st.title("📊 IPO Tracker Tool")
st.caption("Personal use only — SEBI compliance ke liye ye tool online publish nahi karna.")

master_df = load_master()
deleted_df = load_deleted()

# Sidebar controls
st.sidebar.header("Settings")
board_filter = st.sidebar.radio("Board Type", ["All", "Mainboard", "SME"])

if st.sidebar.button("🔄 Manual Refresh (scrape now)"):
    with st.spinner("Data fetch ho raha hai..."):
        master_df = run_daily_update()
    st.sidebar.success("Update ho gaya!")

if st.sidebar.button("🔐 Angel One Login Test"):
    obj = angel_login()
    if obj:
        st.sidebar.success("Angel One login successful!")

# Filter view
view_df = master_df.copy()
if board_filter != "All":
    view_df = view_df[view_df["board_type"] == board_filter]

st.subheader(f"IPO List ({len(view_df)})")

if view_df.empty:
    st.info("Abhi koi data nahi hai. Sidebar se 'Manual Refresh' click karo.")
else:
    # Table with colored score column (basic Streamlit dataframe styling)
    def highlight_score(val):
        try:
            val = float(val)
        except (TypeError, ValueError):
            return ""
        color = score_to_color(val)
        return f"background-color: {color}"

    display_cols = ["company_name", "board_type", "nse_sme_listed", "status",
                     "issue_price_low", "issue_price_high", "issue_pe", "issue_pb",
                     "issue_debt_equity", "score"]
    display_cols = [c for c in display_cols if c in view_df.columns]

    styled = view_df[display_cols].style.applymap(highlight_score, subset=["score"]) \
        if "score" in display_cols else view_df[display_cols].style

    st.dataframe(styled, use_container_width=True)

    # Company detail view (click-like via selectbox, P3)
    st.subheader("Company Detail")
    selected = st.selectbox("Company select karo detail dekhne ke liye", view_df["company_name"].unique())
    if selected:
        row = view_df[view_df["company_name"] == selected].iloc[0]
        col1, col2, col3 = st.columns(3)
        col1.metric("Issue Price", f"₹{row.get('issue_price_low','-')} - ₹{row.get('issue_price_high','-')}")
        col2.metric("Listing Price", f"₹{row.get('listing_price','-')}")
        col3.metric("Current Price", f"₹{row.get('current_price','-')}")

        st.write("**Fundamentals (Issue time vs Current):**")
        detail_table = pd.DataFrame({
            "Metric": ["PE", "PB", "Debt/Equity", "EBITDA Margin", "Market Cap"],
            "At Issue": [row.get("issue_pe"), row.get("issue_pb"),
                         row.get("issue_debt_equity"), row.get("issue_ebitda_margin"),
                         row.get("issue_market_cap")],
            "Current": [None, row.get("current_pb"), row.get("current_debt_equity"),
                        None, row.get("current_market_cap")],
        })
        st.table(detail_table)

        st.write(f"**Score:** {row.get('score','-')}/100")
        st.write(f"**Anchor Lock-in Expiry:** {row.get('anchor_lockin_expiry','-')}")
        st.write(f"**Promoter Lock-in Expiry:** {row.get('promoter_lockin_expiry','-')}")

        # Delete button (P4)
        if st.button(f"🗑️ Delete {selected} (mark as not interested)"):
            deleted_df = pd.concat([deleted_df, pd.DataFrame([{"company_name": selected}])],
                                    ignore_index=True)
            save_csv(deleted_df, DELETED_FILE)
            master_df = master_df[master_df["company_name"] != selected]
            save_csv(master_df, IPO_MASTER_FILE)
            st.success(f"{selected} delete ho gaya. Page refresh karo.")

    # Excel download (P9)
    excel_data = export_to_excel(view_df[display_cols])
    st.download_button(
        label="⬇️ Excel Download Karo",
        data=excel_data,
        file_name=f"ipo_tracker_{dt.date.today().isoformat()}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

st.sidebar.markdown("---")
st.sidebar.caption("Score Legend: 🟢 80+ Strong | 🟢 65-79 Good | ⚪ Normal | 🔴 <40 Weak")
