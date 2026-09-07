# IPO Tracker Tool — Setup Guide (Hindi/Hinglish)

## Files kya hain
- `app.py` — Poora tool (scraping + scoring + Angel One + Telegram + UI)
- `requirements.txt` — Sab libraries ki list
- `.streamlit/secrets.toml.example` — Tumhare API keys/tokens ka template

## Laptop pe Setup (Step by Step)

1. **Python install** karo (agar pehle se nahi hai) — python.org se 3.10+ version

2. **Folder banao aur files daalo:**
   ```
   ipo_tool/
   ├── app.py
   ├── requirements.txt
   └── .streamlit/
       └── secrets.toml   (ye tum khud banaoge, example se copy karke)
   ```

3. **Terminal/CMD mein jao us folder mein**, phir:
   ```
   pip install -r requirements.txt
   ```

4. **secrets.toml banao:**
   - `.streamlit/secrets.toml.example` ko copy karo
   - naam badal ke `secrets.toml` rakho (`.example` hatao)
   - Angel One API key, client ID, password, TOTP secret bharo
   - Telegram bot token aur chat ID bharo (jo tumhare paas already hai)

5. **App chalao:**
   ```
   streamlit run app.py
   ```
   Browser mein khud khul jayega — `http://localhost:8501`

## GitHub Pe Kaise Rakhein (Storage ke liye)

1. GitHub par ek naya **private** repo banao (private rakhna zaroori hai — apna data hai)
2. `.gitignore` file banao aur usme likho:
   ```
   .streamlit/secrets.toml
   ```
   (Isse tumhare secret keys galti se GitHub pe upload nahi honge)
3. Baaki files (app.py, requirements.txt, data/ folder) push kar do
4. Streamlit Cloud pe deploy karte waqt, secrets waha ke "Settings > Secrets"
   mein manually paste karna hoga (waha ki settings mein secrets.toml jaisa
   hi format use hota hai)

## GitHub Actions (Automatic Daily Update) — Baad mein Setup Karenge

Jab tumhara basic tool chal jaye aur test ho jaye, hum ek `.github/workflows/daily_update.yml`
file banayenge jo roz automatically:
- Naye IPO check karegi
- GMP update karegi
- Angel One session refresh karegi
- Telegram alert bhejegi

Isko abhi is version mein nahi rakha hai — pehle basic tool ko apne laptop
pe test karke dekh lo, phir automation wala step next mein karenge.

## Important Notes

- Ye tool **personal use** ke liye hai, SEBI compliance ke liye ise
  public/commercial website pe deploy mat karna.
- Scraper (`scrape_ipo_list`, `scrape_gmp_and_subscription` functions)
  abhi basic hain — InvestorGain jaisi websites ka structure badalte
  rehte hain, isliye agar data aana band ho jaye to sabse pehle wahan
  ke CSS selectors check karne padenge.
- Sector-wise scoring benchmarks (`SECTOR_BENCHMARKS` dictionary app.py
  mein) tum apne experience ke hisaab se tune kar sakte ho.
