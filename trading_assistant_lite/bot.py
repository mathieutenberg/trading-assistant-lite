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
KEYBOARD = {"keyboard": [[{"text": "📈 Koersen"}, {"text": "🌍 Markt"}], [{"text": "📰 Nieuws"}, {"text": "⭐ Watchlist"}], [{"text": "🤖 AI"}, {"text": "❓ Help"}]], "resize_keyboard": True}


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
        return {"watchlist": state.get("watchlist", DEFAULT_WATCHLIST), "ai_next": bool(state.get("ai_next", False)), "offset": int(state.get("offset", 0))}
    except (OSError, ValueError, TypeError):
        return {"watchlist": DEFAULT_WATCHLIST.copy(), "ai_next": False, "offset": 0}


def save_state(state):
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False))
    temp.replace(STATE_FILE)


def finnhub(path, params=None):
    if not FINNHUB:
        raise RuntimeError("Finnhub API-sleutel ontbreekt in de app-configuratie.")
    params = {**(params or {}), "token": FINNHUB}
    return request_json("https://finnhub.io/api/v1/" + path + "?" + urllib.parse.urlencode(params))


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
            f"Laatste koersupdate: {stamp}\nKoersen kunnen vertraagd zijn afhankelijk van je datalicentie.")


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
    if low in ("/start", "/help", "❓ help", "help"):
        return ("Trading Assistant Lite\n\n"
                "Typ NVDA, AAPL, QQQ enz. voor een koers.\n"
                "/quote NVDA · /news NVDA · /market\n"
                "/watchlist · /add AMD · /remove AMD\n"
                "/ai Wat is een ETF?\n"
                "🤖 AI-knop: alleen de volgende vraag gaat naar OpenAI.\n"
                "📰 Nieuws: recente Finnhub-headlines met links.")
    if low in ("🤖 ai", "/ai"):
        state["ai_next"] = True
        return "🤖 Stel je volgende vraag. Alleen die vraag gaat naar OpenAI. /cancel om te annuleren."
    if low == "/cancel":
        state["ai_next"] = False
        return "AI-vraag geannuleerd."
    if low.startswith("/ai "):
        state["ai_next"] = False
        return ask_ai(raw[4:].strip())
    if state["ai_next"]:
        state["ai_next"] = False
        return ask_ai(raw)
    if low in ("🌍 markt", "/market", "markt"):
        return market()
    if low in ("📰 nieuws", "/news", "nieuws"):
        return news()
    if low.startswith("/news ") or low.startswith("nieuws "):
        symbol = raw.split(maxsplit=1)[1].strip().upper()
        return news(symbol) if valid_symbol(symbol) else "Ongeldig tickersymbool."
    if low in ("⭐ watchlist", "/watchlist", "watchlist"):
        lines = ["⭐ Watchlist"]
        for symbol in state["watchlist"][:15]:
            try:
                d = finnhub("quote", {"symbol": symbol})
                lines.append(f"{symbol}: ${d.get('c', 0):.2f} ({d.get('dp', 0):+.2f}%)" if d.get("c", 0) else f"{symbol}: niet beschikbaar")
            except Exception:
                lines.append(f"{symbol}: tijdelijk niet beschikbaar")
        return "\n".join(lines)
    if low.startswith("/add "):
        symbol = raw.split(maxsplit=1)[1].upper().strip()
        if not valid_symbol(symbol):
            return "Ongeldig tickersymbool."
        if symbol not in state["watchlist"]:
            if len(state["watchlist"]) >= 25:
                return "Maximaal 25 aandelen in de watchlist."
            state["watchlist"].append(symbol)
        return f"{symbol} staat op je watchlist."
    if low.startswith("/remove "):
        symbol = raw.split(maxsplit=1)[1].upper().strip()
        state["watchlist"] = [x for x in state["watchlist"] if x != symbol]
        return f"{symbol} verwijderd (als het op de lijst stond)."
    if low == "📈 koersen":
        return "Typ een ticker zoals NVDA, AAPL, SPY of QQQ."
    if low.startswith("/quote "):
        raw = raw.split(maxsplit=1)[1].strip()
    if valid_symbol(raw):
        return quote(raw.upper())
    return "Onbekende opdracht. Typ /help. Voor ChatGPT gebruik je 🤖 AI of /ai jouw vraag."


def main():
    if not TOKEN or not CHAT or not FINNHUB:
        raise SystemExit("Vul telegram_bot_token, telegram_chat_id en finnhub_api_key in bij de app-configuratie.")
    state = load_state()
    LOG.info("Trading Assistant Lite v1.0.2 gestart (alleen geautoriseerde chat).")
    try:
        me = telegram("getMe", {})
        LOG.info("Telegram API bereikbaar; bot: @%s", me.get("username", "onbekend"))
    except Exception:
        LOG.exception("Telegram getMe mislukt; controleer de bot-token")
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
