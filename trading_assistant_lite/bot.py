129
130
131
132
133
134
135
136
137
138
139
140
141
142
143
144
145
146
147
148
149
150
151
152
153
154
155
156
157
158
159
160
161
162
163
164
165
166
167
168
169
170
171
172
173
174
175
176
177
178
179
180
181
182
183
184
185
186
187
188
189
190
191
192
193
194
195
196
197
198
199
200
201
202
203
204
205
206
207
208
209
210
211
212
213
214
215
216
217
218
219
220
221
222
223
224
225
226
227
228
229
230
231
232
233
234
235
236
237
238
239
240
241
242
243
244
245
246
247
248
249
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
    LOG.info("Trading Assistant Lite v1.0.1 gestart (alleen geautoriseerde chat).")
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
