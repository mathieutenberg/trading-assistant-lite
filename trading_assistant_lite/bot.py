"""Telegram Trading Assistant Lite: standalone Home Assistant add-on.
No third-party Python dependencies. API keys remain in add-on options.
"""
import datetime as dt
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zoneinfo
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("trading-lite")
DATA = Path("/data")
DATA.mkdir(parents=True, exist_ok=True)
OPTIONS = json.loads((DATA / "options.json").read_text())
TOKEN = OPTIONS.get("telegram_bot_token", "").strip()
CHAT = str(OPTIONS.get("telegram_chat_id", "")).strip()
FINNHUB = OPTIONS.get("finnhub_api_key", "").strip()
OPENAI = OPTIONS.get("openai_api_key", "").strip()
MODEL = OPTIONS.get("openai_model", "gpt-4.1-mini").strip() or "gpt-4.1-mini"
STATE_FILE = DATA / "trading_state.json"
DEFAULT_WATCHLIST = ["AAPL", "NVDA", "MSFT", "QQQ", "SPY"]
KEYBOARD = {"keyboard": [
    ["📊 Overzicht", "📈 Koersen"],
    ["⭐ Watchlist", "➕ Toevoegen", "➖ Verwijderen"],
    ["🏢 Company", "📅 Earnings"],
    ["🏦 Institutions", "🎯 Analysts"],
    ["📊 Financials", "⭐ IBD Overview"],
    ["📰 Stock News", "🌍 Market News"],
    ["🤖 AI", "⚙️ Instellingen"],
    ["❓ Help"]
], "resize_keyboard": True, "is_persistent": True}


def request_json(url, payload=None, headers=None, timeout=25):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "TradingAssistantLite/1.0", **({"Content-Type": "application/json"} if payload is not None else {}), **(headers or {})}, method="POST" if payload is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:250]
        raise RuntimeError(f"API HTTP {exc.code}: {body}") from exc


def telegram(method, payload):
    try:
        response = request_json(f"https://api.telegram.org/bot{TOKEN}/{method}", payload, timeout=45)
    except Exception as exc:
        LOG.error("Telegram API-fout bij methode %s: %s", method, str(exc).replace(TOKEN, "[TOKEN]"))
        raise
    if not response.get("ok"):
        raise RuntimeError(f"Telegram: {response.get('description', 'unknown error')}")
    return response.get("result")


def send(text, keyboard=True):
    # Never call sendMessage with empty text.
    text = str(text or "").strip()
    if not text:
        LOG.warning("Leeg antwoord geblokkeerd; geen Telegram-bericht verstuurd.")
        return
    # Telegram messages max 4096 characters; keep safe chunks.
    for i in range(0, len(text), 3800):
        payload = {"chat_id": CHAT, "text": text[i:i + 3800]}
        if keyboard:
            payload["reply_markup"] = KEYBOARD
        telegram("sendMessage", payload)


def load_state():
    try:
        state = json.loads(STATE_FILE.read_text())
        return {"watchlist": state.get("watchlist", DEFAULT_WATCHLIST), "ai_next": bool(state.get("ai_next", False)), "offset": int(state.get("offset", 0)), "pending": state.get("pending", ""), "menu_version": state.get("menu_version", ""), "selected": state.get("selected", "NVDA")}
    except (OSError, ValueError, TypeError):
        return {"watchlist": DEFAULT_WATCHLIST.copy(), "ai_next": False, "offset": 0, "pending": "", "menu_version": "", "selected": "NVDA"}


def save_state(state):
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False))
    temp.replace(STATE_FILE)


def finnhub(path, params=None):
    if not FINNHUB:
        raise RuntimeError("Finnhub API-sleutel ontbreekt in de app-configuratie.")
    params = {**(params or {}), "token": FINNHUB}
    return request_json("https://finnhub.io/api/v1/" + path + "?" + urllib.parse.urlencode(params))


def extra_hours(symbol):
    """Best-effort public Yahoo Finance chart data; not guaranteed/live or licensed."""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(symbol, safe="") + "?interval=1m&range=1d&includePrePost=true"
    try:
        data = request_json(url, headers={"User-Agent": "Mozilla/5.0 (compatible; TradingAssistantLite/1.1)"}, timeout=12)
        result = (data.get("chart", {}).get("result") or [])[0]
        meta = result.get("meta", {})
        tz = zoneinfo.ZoneInfo("America/New_York")
        now = dt.datetime.now(tz)
        # Only show session prices when a matching timestamp exists TODAY in New York.
        ts = result.get("timestamp") or []
        quotes = ((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        session = "premarket" if dt.time(4) <= now.time() < dt.time(9,30) else ("after-hours" if dt.time(16) <= now.time() < dt.time(20) else None)
        if not session or now.weekday() >= 5:
            return "Buiten reguliere handel: momenteel geen actieve premarket/after-hours-sessie."
        matches = []
        for t, price in zip(ts, quotes):
            if not isinstance(price, (int,float)) or price <= 0:
                continue
            at = dt.datetime.fromtimestamp(t, tz)
            if at.date() != now.date():
                continue
            if session == "premarket" and dt.time(4) <= at.time() < dt.time(9,30):
                matches.append((at,price))
            if session == "after-hours" and dt.time(16) <= at.time() < dt.time(20):
                matches.append((at,price))
        if not matches:
            return f"{session.title()}: geen verifieerbare koers beschikbaar."
        at, price = matches[-1]
        if (now - at).total_seconds() > 15*60:
            return f"{session.title()}: geen recente koers beschikbaar (laatste tick {at:%H:%M} ET)."
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        pct = f" ({(price/prev-1)*100:+.2f}% t.o.v. vorige slotkoers)" if isinstance(prev,(int,float)) and prev>0 else ""
        return f"{session.title()}: ${price:,.2f}{pct} · {at:%H:%M} ET (Yahoo, indicatief)"
    except Exception as exc:
        LOG.info("Extended-hours bron niet beschikbaar voor %s: %s", symbol, str(exc)[:100])
        return "Premarket/after-hours: momenteel niet beschikbaar via de aanvullende bron."


def quote(symbol):
    data = finnhub("quote", {"symbol": symbol})
    price = data.get("c")
    if not isinstance(price, (float, int)) or price <= 0:
        return f"Geen geldige koers gevonden voor {symbol}."
    change = data.get("dp")
    sign = "+" if isinstance(change, (int, float)) and change > 0 else ""
    change_text = f"{sign}{change:.2f}%" if isinstance(change, (int, float)) else "onbekend"
    stamp = dt.datetime.fromtimestamp(data["t"], dt.timezone.utc).strftime("%d-%m %H:%M UTC") if data.get("t") else "onbekend"
    return (f"📈 {symbol}: ${price:,.2f} ({change_text})\n"
            f"Open: ${data.get('o', 0):,.2f} | Hoog: ${data.get('h', 0):,.2f} | Laag: ${data.get('l', 0):,.2f}\n"
            f"Finnhub koersupdate: {stamp}\n{extra_hours(symbol)}\n"
            "Koersen kunnen vertraagd zijn; aanvullende bron is indicatief.")


def overview(state):
    symbol = state.get("selected", "NVDA")
    return (f"📊 Trading Assistant Lite v1.2.0\nSelected: {symbol}\n"
            f"Watchlist: {len(state['watchlist'])} tickers\n\n"
            "Type NVDA (or another ticker) to select a stock and view its quote.\n"
            "Use Company, Earnings, Institutions, Analysts, Financials, IBD Overview and Stock News for the selected ticker.\n"
            "Some datasets may be unavailable on Finnhub Free; dates and sources are shown where possible.")


def readable_time(ts):
    try:
        return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, TypeError, OverflowError):
        return "date unavailable"


def safe_data(path, params=None):
    try:
        return finnhub(path, params), None
    except Exception as exc:
        LOG.info("Finnhub %s unavailable: %s", path, str(exc)[:120])
        return None, "Not available on this data plan or temporarily unavailable."


def fmt_num(value, digits=2):
    return f"{value:,.{digits}f}" if isinstance(value, (int, float)) else "N/A"


def company(symbol):
    d, error = safe_data("stock/profile2", {"symbol": symbol})
    if not isinstance(d, dict) or not d.get("name"):
        return f"🏢 {symbol} · Company\n{error or 'Company profile unavailable.'}"
    return (f"🏢 {d.get('name')} ({symbol})\n"
            f"Industry: {d.get('finnhubIndustry') or 'N/A'}\n"
            f"Country: {d.get('country') or 'N/A'} · Exchange: {d.get('exchange') or 'N/A'}\n"
            f"Market cap: {fmt_num(d.get('marketCapitalization'))} million {d.get('currency') or ''}\n"
            f"Shares outstanding: {fmt_num(d.get('shareOutstanding'))} million\n"
            f"Website: {d.get('weburl') or 'N/A'}\n"
            "Business description and competitors: not supplied by this Finnhub endpoint.")


def earnings(symbol):
    lines = [f"📅 {symbol} · Earnings"]
    cal, err = safe_data("calendar/earnings", {"symbol": symbol, "from": str(dt.date.today()), "to": str(dt.date.today() + dt.timedelta(days=180))})
    rows = cal.get("earningsCalendar", []) if isinstance(cal, dict) else []
    upcoming = sorted((x for x in rows if x.get("date")), key=lambda x: x["date"])
    if upcoming:
        x = upcoming[0]
        lines.append(f"Next reported/estimated date: {x.get('date')} ({x.get('hour') or 'time TBA'})")
        lines.append(f"EPS estimate: {x.get('epsEstimate') if x.get('epsEstimate') is not None else 'N/A'} · Revenue estimate: {x.get('revenueEstimate') if x.get('revenueEstimate') is not None else 'N/A'}")
    else:
        lines.append("Next earnings date: not available/confirmed via current source.")
    past, err = safe_data("stock/earnings", {"symbol": symbol, "limit": 4})
    if isinstance(past, list) and past:
        lines.append("\nRecent reported EPS (actual / estimate / surprise):")
        for x in past[:4]:
            lines.append(f"{x.get('period','N/A')}: {x.get('actual','N/A')} / {x.get('estimate','N/A')} / {x.get('surprisePercent','N/A')}%")
    else:
        lines.append("Historical EPS results unavailable on current source/plan.")
    return "\n".join(lines)


def analysts(symbol):
    lines = [f"🎯 {symbol} · Analysts"]
    targets, _ = safe_data("stock/price-target", {"symbol": symbol})
    if isinstance(targets, dict) and targets.get("targetMean") is not None:
        lines.extend([f"Target mean: ${fmt_num(targets.get('targetMean'))}",
                      f"Low / High: ${fmt_num(targets.get('targetLow'))} / ${fmt_num(targets.get('targetHigh'))}",
                      f"Target update: {targets.get('lastUpdated') or 'N/A'}"])
    else:
        lines.append("Price targets unavailable on current plan.")
    rec, _ = safe_data("stock/recommendation", {"symbol": symbol})
    if isinstance(rec, list) and rec:
        x = rec[0]
        lines.append(f"\nRecommendations ({x.get('period','N/A')}): Strong buy {x.get('strongBuy',0)}, Buy {x.get('buy',0)}, Hold {x.get('hold',0)}, Sell {x.get('sell',0)}, Strong sell {x.get('strongSell',0)}")
    else:
        lines.append("Recommendation consensus unavailable.")
    changes, _ = safe_data("stock/upgrade-downgrade", {"symbol": symbol})
    if isinstance(changes, list) and changes:
        lines.append("\nLatest rating changes:")
        for x in sorted(changes, key=lambda z: z.get("gradeTime", 0), reverse=True)[:4]:
            lines.append(f"{readable_time(x.get('gradeTime'))[:10]} · {x.get('company','N/A')} · {x.get('action','N/A')}: {x.get('fromGrade','?')} → {x.get('toGrade','?')}")
    else:
        lines.append("Individual upgrades/downgrades unavailable on current plan.")
    return "\n".join(lines)


def institutions(symbol):
    lines = [f"🏦 {symbol} · Institutional Ownership"]
    holders, _ = safe_data("stock/institutional-ownership", {"symbol": symbol})
    rows = holders.get("ownership", []) if isinstance(holders, dict) else []
    if rows:
        lines.append(f"Reported institutional records: {len(rows)} (not necessarily total number of unique institutions)")
        for x in rows[:8]:
            lines.append(f"{x.get('name') or x.get('investorName') or 'Institution'}: {x.get('share') or x.get('shares') or 'N/A'} shares · filed {x.get('reportDate') or x.get('filingDate') or 'N/A'}")
    else:
        lines.append("Holder count and holdings are not available from the current Finnhub access.")
    lines.append("13F institutional filings are delayed; never treat these as live holdings.")
    return "\n".join(lines)


def financials(symbol):
    lines = [f"📊 {symbol} · Financials"]
    metrics, _ = safe_data("stock/metric", {"symbol": symbol, "metric": "all"})
    m = metrics.get("metric", {}) if isinstance(metrics, dict) else {}
    if not m:
        return "\n".join(lines + ["Financial metrics unavailable on current source/plan."])
    for label, keys in [
        ("P/E TTM", ["peTTM"]), ("EPS TTM", ["epsTTM"]),
        ("Revenue growth 3Y (%)", ["revenueGrowth3Y"]),
        ("Revenue growth 5Y (%)", ["revenueGrowth5Y"]),
        ("EPS growth 3Y (%)", ["epsGrowth3Y"]),
        ("EPS growth 5Y (%)", ["epsGrowth5Y"]),
        ("Net margin TTM (%)", ["netProfitMarginTTM"]),
        ("ROE TTM (%)", ["roeTTM"])]:
        v = next((m[k] for k in keys if m.get(k) is not None), None)
        lines.append(f"{label}: {fmt_num(v) if v is not None else 'N/A'}")
    return "\n".join(lines)


def ibd_overview(symbol):
    lines = [f"⭐ {symbol} · IBD-style Overview (unofficial)"]
    metrics, _ = safe_data("stock/metric", {"symbol": symbol, "metric": "all"})
    m = metrics.get("metric", {}) if isinstance(metrics, dict) else {}
    for label, key in [("52-week high", "52WeekHigh"), ("52-week low", "52WeekLow"),
                       ("52-week price return (%)", "52WeekPriceReturnDaily"),
                       ("3-month price return (%)", "3MonthPriceReturnDaily"),
                       ("EPS growth 3Y (%)", "epsGrowth3Y"),
                       ("Revenue growth 3Y (%)", "revenueGrowth3Y")]:
        lines.append(f"{label}: {fmt_num(m.get(key))}")
    lines.append("Official IBD RS, Composite and Acc/Dis ratings are proprietary and NOT calculated here.")
    lines.append("Trend, volume breakout and institutional accumulation scores require historical OHLCV data.")
    return "\n".join(lines)

def market():
    lines = ["🌍 Markt (ETF-proxies, niet de officiële indexstanden):"]
    for symbol, label in [("SPY", "S&P 500"), ("QQQ", "Nasdaq 100"), ("DIA", "Dow Jones"), ("IWM", "Russell 2000")]:
        try:
            d = finnhub("quote", {"symbol": symbol})
            if d.get("c", 0) > 0:
                lines.append(f"{label} ({symbol}): ${d['c']:.2f} | {d.get('dp', 0):+.2f}%")
            else:
                lines.append(f"{label}: geen koers beschikbaar")
        except Exception:
            lines.append(f"{label}: tijdelijk niet beschikbaar")
    lines.append("Sentiment-indicatie: bekijk de richting van deze ETF's; geen gevalideerde sentimentindex.")
    return "\n".join(lines)


def news(symbol=None):
    if symbol:
        today = dt.datetime.now(dt.timezone.utc).date()
        articles = finnhub("company-news", {"symbol": symbol, "from": str(today - dt.timedelta(days=7)), "to": str(today)})
        heading = f"📰 Nieuws over {symbol}"
    else:
        articles = finnhub("news", {"category": "general"})
        heading = "📰 Algemeen marktnieuws"
    if not isinstance(articles, list) or not articles:
        return heading + "\nGeen recente berichten beschikbaar."
    lines = [heading]
    for item in articles[:5]:
        headline = str(item.get("headline", "Zonder titel"))[:180]
        source = item.get("source", "onbekend")
        timestamp = item.get("datetime")
        published = dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).strftime("%d-%m %H:%M UTC") if timestamp else "tijd onbekend"
        link = item.get("url", "")
        lines.append(f"\n• {headline}\n{source} · {published}\n{link}")
    return "\n".join(lines)


def ask_ai(question):
    if not OPENAI:
        return "De OpenAI API-sleutel ontbreekt. Voeg deze toe in de app-configuratie."
    payload = {"model": MODEL, "messages": [
        {"role": "system", "content": "Je bent een Nederlandstalige, beknopte assistent voor algemene vragen en beleggen. Wees nauwkeurig. Je hebt geen live webtoegang; verzin geen actuele prijzen of nieuws. Voor actuele koersen verwijs naar de Finnhub-functies van deze bot. Geef geen persoonlijk beleggingsadvies."},
        {"role": "user", "content": question}], "max_tokens": 900}
    data = request_json("https://api.openai.com/v1/chat/completions", payload,
                        {"Authorization": f"Bearer {OPENAI}", "Content-Type": "application/json"}, timeout=55)
    return data["choices"][0]["message"]["content"] or "Geen antwoord ontvangen."


def valid_symbol(text):
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9.\-]{0,11}", text))


def handle(text, state):
    raw = text.strip()
    low = raw.lower()
    symbol = state.get("selected", "NVDA")
    if low in ("/cancel", "❌ annuleren"):
        state["ai_next"] = False
        state["pending"] = ""
        return "Cancelled."
    if low in ("/start", "/help", "❓ help", "help"):
        return ("Trading Assistant Lite v1.2.0\n\n"
                "Type NVDA to select a stock and get its quote.\n"
                "Company · Earnings · Institutions · Analysts · Financials · IBD Overview · Stock News use your selected ticker.\n"
                "/news NVDA · /company NVDA · /earnings NVDA · /analysts NVDA\n"
                "/institutions NVDA · /financials NVDA · /ibd NVDA\n"
                "/watchlist · /add AMD · /remove AMD · /market\n"
                "AI: /ai your question · /cancel to cancel.")
    if low in ("🤖 ai", "/ai"):
        state["pending"] = ""
        state["ai_next"] = True
        return "🤖 Ask your next question. /cancel to cancel."
    if low.startswith("/ai "):
        state["ai_next"] = False
        return ask_ai(raw[4:].strip())
    if state.get("ai_next"):
        state["ai_next"] = False
        return ask_ai(raw)
    if low in ("➕ toevoegen", "toevoegen"):
        state["pending"] = "add"
        return "Ticker to add? e.g. AMD. /cancel to cancel."
    if low in ("➖ verwijderen", "verwijderen"):
        state["pending"] = "remove"
        return "Ticker to remove? e.g. AMD. /cancel to cancel."
    if state.get("pending") in ("add", "remove") and valid_symbol(raw) and not raw.startswith("/"):
        action = state["pending"]
        state["pending"] = ""
        return watchlist_change(raw.upper(), state, action)
    if low.startswith("/add "):
        return watchlist_change(raw.split(maxsplit=1)[1].upper(), state, "add")
    if low.startswith("/remove "):
        return watchlist_change(raw.split(maxsplit=1)[1].upper(), state, "remove")
    if low in ("📊 overzicht", "overzicht", "/overview"):
        return overview(state)
    if low in ("⚙️ instellingen", "instellingen", "/settings"):
        return ("⚙️ Settings\n"
                f"OpenAI: {'configured' if OPENAI else 'not configured'}\n"
                f"Finnhub: {'configured' if FINNHUB else 'not configured'}\n"
                f"AI model: {MODEL}\nSelected: {symbol}\n"
                "Free Finnhub API availability varies by endpoint; missing data is reported explicitly.")
    if low in ("🌍 markt", "🌍 market news", "/market", "markt"):
        return market() if low != "🌍 market news" else news()
    if low in ("📰 nieuws", "📰 stock news", "/news", "nieuws"):
        return news(symbol)
    if low.startswith("/news ") or low.startswith("nieuws "):
        candidate = raw.split(maxsplit=1)[1].strip().upper()
        if not valid_symbol(candidate):
            return "Invalid ticker."
        state["selected"] = candidate
        return news(candidate)
    if low in ("⭐ watchlist", "/watchlist", "watchlist"):
        lines = ["⭐ Watchlist"]
        for ticker in state["watchlist"][:25]:
            try:
                d = finnhub("quote", {"symbol": ticker})
                lines.append(f"{ticker}: ${d.get('c', 0):.2f} ({d.get('dp', 0):+.2f}%)" if d.get("c", 0) else f"{ticker}: unavailable")
            except Exception:
                lines.append(f"{ticker}: temporarily unavailable")
        return "\n".join(lines)
    if low == "📈 koersen":
        return f"Selected: {symbol}. Type NVDA or another ticker for a quote."
    commands = [
        (("🏢 company", "/company"), company),
        (("📅 earnings", "/earnings"), earnings),
        (("🏦 institutions", "/institutions"), institutions),
        (("🎯 analysts", "/analysts"), analysts),
        (("📊 financials", "/financials"), financials),
        (("⭐ ibd overview", "/ibd"), ibd_overview),
    ]
    for aliases, fn in commands:
        if low in aliases:
            return fn(symbol)
        for alias in aliases:
            if alias.startswith("/") and low.startswith(alias + " "):
                candidate = raw[len(alias):].strip().upper()
                if not valid_symbol(candidate):
                    return "Invalid ticker."
                state["selected"] = candidate
                return fn(candidate)
    if low.startswith("/quote "):
        raw = raw.split(maxsplit=1)[1].strip()
    if valid_symbol(raw):
        state["selected"] = raw.upper()
        return quote(raw.upper()) + "\n\nUse Company, Earnings, Institutions, Analysts, Financials, IBD Overview or Stock News for more."
    return "Unknown command. Type /help."


def watchlist_change(symbol, state, action):
    symbol = symbol.strip().upper()
    if not valid_symbol(symbol):
        return "Ongeldig tickersymbool. Gebruik bijvoorbeeld AMD."
    if action == "add":
        if symbol not in state["watchlist"]:
            if len(state["watchlist"]) >= 25:
                return "Maximaal 25 aandelen in de watchlist."
            state["watchlist"].append(symbol)
        return f"{symbol} staat op je watchlist."
    state["watchlist"] = [x for x in state["watchlist"] if x != symbol]
    return f"{symbol} verwijderd (als het op de lijst stond)."


def main():
    if not TOKEN or not CHAT or not FINNHUB:
        raise SystemExit("Vul telegram_bot_token, telegram_chat_id en finnhub_api_key in bij de app-configuratie.")
    state = load_state()
    LOG.info("Trading Assistant Lite v1.2.0 gestart (alleen geautoriseerde chat).")
    try:
        me = telegram("getMe", {})
        LOG.info("Telegram API bereikbaar; bot: @%s", me.get("username", "onbekend"))
    except Exception:
        LOG.exception("Telegram getMe mislukt; controleer de bot-token")
    if state.get("menu_version") != "1.2.0":
        try:
            send("✅ Trading Assistant Lite v1.2.0 actief. Het nieuwe menu staat hieronder.")
            state["menu_version"] = "1.2.0"
            save_state(state)
        except Exception:
            LOG.exception("Nieuw Telegram-menu nog niet verzonden")
    while True:
        try:
            updates = telegram("getUpdates", {"offset": state["offset"], "timeout": 25, "allowed_updates": ["message"]})
            for update in updates:
                state["offset"] = max(state["offset"], update["update_id"] + 1)
                msg = update.get("message") or {}
                if str((msg.get("chat") or {}).get("id", "")) != CHAT:
                    LOG.warning("Bericht van niet-toegestane chat genegeerd")
                    save_state(state)
                    continue
                content = msg.get("text", "")
                LOG.info("Telegram-bericht ontvangen: chat_id=%s, opdracht=%s", (msg.get("chat") or {}).get("id"), str(content)[:60])
                if not content:
                    save_state(state)
                    continue
                try:
                    reply = handle(content, state)
                except Exception as exc:
                    LOG.exception("Fout tijdens verwerken bericht")
                    reply = f"Er ging iets mis: {str(exc)[:180]}"
                save_state(state)
                LOG.info("Antwoord verzenden: %s tekens", len(str(reply or "")))
                send(reply)
        except Exception as exc:
            LOG.exception("Telegram-verbinding mislukt: %s", str(exc)[:200])
            time.sleep(8)


if __name__ == "__main__":
    main()
