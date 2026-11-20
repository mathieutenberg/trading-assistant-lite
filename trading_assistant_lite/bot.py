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
    ["🎯 Scanner", "🌍 Markt"],
    ["📰 Nieuws", "🤖 AI"],
    ["⚙️ Instellingen", "❓ Help"]
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
        return {"watchlist": state.get("watchlist", DEFAULT_WATCHLIST), "ai_next": bool(state.get("ai_next", False)), "offset": int(state.get("offset", 0)), "pending": state.get("pending", ""), "menu_version": state.get("menu_version", "")}
    except (OSError, ValueError, TypeError):
        return {"watchlist": DEFAULT_WATCHLIST.copy(), "ai_next": False, "offset": 0, "pending": "", "menu_version": ""}


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


def scanner(state):
    symbols = list(dict.fromkeys(state["watchlist"]))[:25]
    rows = []
    for symbol in symbols:
        try:
            d = finnhub("quote", {"symbol": symbol})
            if d.get("c",0) and isinstance(d.get("dp"), (float,int)):
                rows.append((d["dp"],symbol,d["c"]))
        except Exception:
            pass
    if not rows:
        return "🎯 Scanner: geen koersdata beschikbaar voor je watchlist."
    rows.sort(reverse=True)
    lines = ["🎯 Scanner · watchlist op dagprestatie", "Geen marktbrede of technische breakoutscanner."]
    for pct, symbol, price in rows[:15]:
        lines.append(f"{symbol}: ${price:,.2f} ({pct:+.2f}%)")
    return "\n".join(lines)


def overview(state):
    return ("📊 Trading Assistant Lite\n"
            f"Watchlist: {len(state['watchlist'])} tickers\n"
            "Typ NVDA voor koers en eventuele extended-hours-data.\n"
            "Gebruik 🎯 Scanner voor een ranglijst van je watchlist.\n"
            "Gebruik 🤖 AI voor een vraag.")


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
    if low in ("/cancel", "❌ annuleren"):
        state["ai_next"] = False
        state["pending"] = ""
        return "Geannuleerd."
    if low in ("/start", "/help", "❓ help", "help"):
        state["pending"] = ""
        return ("Trading Assistant Lite v1.1.0\n\n"
                "📈 NVDA of /quote NVDA: koers + beschikbare premarket/after-hours\n"
                "⭐ /watchlist · ➕ /add AMD · ➖ /remove AMD\n"
                "🎯 /scanner: watchlist op dagprestatie\n"
                "🌍 /market · 📰 /news NVDA\n"
                "🤖 /ai Wat is een ETF?\n"
                "⚙️ /settings: instellingen en databronnen\n"
                "Gebruik /cancel om een invoer te annuleren.")
    if low in ("🤖 ai", "/ai"):
        state["pending"] = ""
        state["ai_next"] = True
        return "🤖 Stel je volgende vraag. /cancel om te annuleren."
    if low.startswith("/ai "):
        state["ai_next"] = False
        return ask_ai(raw[4:].strip())
    if state.get("ai_next"):
        state["ai_next"] = False
        return ask_ai(raw)
    if low in ("➕ toevoegen", "toevoegen"):
        state["pending"] = "add"
        return "Welk tickersymbool toevoegen? Bijvoorbeeld AMD. /cancel om te annuleren."
    if low in ("➖ verwijderen", "verwijderen"):
        state["pending"] = "remove"
        return "Welk tickersymbool verwijderen? Bijvoorbeeld AMD. /cancel om te annuleren."
    if state.get("pending") in ("add", "remove") and not raw.startswith("/") and not raw.startswith(("📊","📈","⭐","🎯","🌍","📰","⚙️","❓","➕","➖")):
        action = state["pending"]
        state["pending"] = ""
        return watchlist_change(raw.upper(), state, action)
    if low.startswith("/add "):
        return watchlist_change(raw.split(maxsplit=1)[1].upper(), state, "add")
    if low.startswith("/remove "):
        return watchlist_change(raw.split(maxsplit=1)[1].upper(), state, "remove")
    if low in ("📊 overzicht", "overzicht", "/overview"):
        return overview(state)
    if low in ("🎯 scanner", "scanner", "/scanner"):
        return scanner(state)
    if low in ("⚙️ instellingen", "instellingen", "/settings"):
        return ("⚙️ Instellingen\n"
                f"OpenAI: {'ingesteld' if OPENAI else 'niet ingesteld'}\n"
                f"Finnhub: {'ingesteld' if FINNHUB else 'niet ingesteld'}\n"
                f"AI-model: {MODEL}\n"
                "Koersen: Finnhub; extended hours: aanvullende Yahoo chartbron (best effort).\n"
                "Wijzig API-sleutels in Home Assistant → Apps → Trading Assistant Lite → Configuratie.")
    if low in ("🌍 markt", "/market", "markt"):
        return market()
    if low in ("📰 nieuws", "/news", "nieuws"):
        return news()
    if low.startswith("/news ") or low.startswith("nieuws "):
        symbol = raw.split(maxsplit=1)[1].strip().upper()
        return news(symbol) if valid_symbol(symbol) else "Ongeldig tickersymbool."
    if low in ("⭐ watchlist", "/watchlist", "watchlist"):
        lines = ["⭐ Watchlist"]
        for symbol in state["watchlist"][:25]:
            try:
                d = finnhub("quote", {"symbol": symbol})
                lines.append(f"{symbol}: ${d.get('c', 0):.2f} ({d.get('dp', 0):+.2f}%)" if d.get("c", 0) else f"{symbol}: niet beschikbaar")
            except Exception:
                lines.append(f"{symbol}: tijdelijk niet beschikbaar")
        return "\n".join(lines)
    if low == "📈 koersen":
        return "Typ een ticker zoals NVDA, AAPL, SPY of QQQ."
    if low.startswith("/quote "):
        raw = raw.split(maxsplit=1)[1].strip()
    if valid_symbol(raw):
        return quote(raw.upper())
    return "Onbekende opdracht. Typ /help."


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
    LOG.info("Trading Assistant Lite v1.1.0 gestart (alleen geautoriseerde chat).")
    try:
        me = telegram("getMe", {})
        LOG.info("Telegram API bereikbaar; bot: @%s", me.get("username", "onbekend"))
    except Exception:
        LOG.exception("Telegram getMe mislukt; controleer de bot-token")
    if state.get("menu_version") != "1.1.0":
        try:
            send("✅ Trading Assistant Lite v1.1.0 actief. Het nieuwe menu staat hieronder.")
            state["menu_version"] = "1.1.0"
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
