# Trading Assistant Lite — Home Assistant OS

Zelfstandige Home Assistant-app met Telegram, Finnhub en optionele OpenAI-vragen.

## Installatie
1. Upload `repository.yaml` en de map `trading_assistant_lite` naar de root van een **openbare** GitHub-repository.
2. Pas optioneel de URL in `repository.yaml` aan naar je eigen GitHub-repository.
3. Home Assistant → Instellingen → Apps → App Store → Repositories → voeg de GitHub-repository-URL toe.
4. Zoek `Trading Assistant Lite`, installeer de app en open Configuratie.
5. Vul `telegram_bot_token`, `telegram_chat_id`, `finnhub_api_key` en eventueel `openai_api_key` in.
6. Start de app, schakel `Bij systeemstart starten` in en bekijk de logs.
7. Stop je oude Render-worker voordat je deze app start, anders concurreren twee processen om Telegram-updates.

## Gebruik
- `NVDA` / `QQQ`: laatste Finnhub-koers met timestamp
- `/market`: ETF-proxies SPY, QQQ, DIA en IWM
- `/news` en `/news NVDA`: recente Finnhub-headlines
- `/watchlist`, `/add AMD`, `/remove AMD`
- `🤖 AI` of `/ai vraag`: optionele OpenAI API (geen live web search)

## Veiligheid en beperkingen
- De bot verwerkt uitsluitend berichten van de opgegeven Telegram-chat-ID.
- API-sleutels horen uitsluitend in de app-configuratie, niet in GitHub.
- Het OpenAI API-abonnement is apart van ChatGPT Plus en kan kosten veroorzaken.
- Finnhub-koersen kunnen vertraagd zijn of niet beschikbaar voor bepaalde symbolen.
- Dit is een eerste eenvoudige versie, zonder pushberichten of automatische handelsorders.
- Maak Home Assistant-back-ups. Watchlist wordt bewaard onder `/data/trading_state.json`.
