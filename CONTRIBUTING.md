# Working on Ordr

Read `CLAUDE.md` first. Using Antigravity or another AI tool? Tell it to read `CLAUDE.md` too.

## Setup (Windows, Python 3.11 or newer)

```
git clone https://github.com/Sky00raider/Ordr.git
cd Ordr
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
copy .env.example .env
pytest
```

## Who owns what

Full task list: `docs/TRD.md`, section 1.

| Folder | Owner | Job |
|---|---|---|
| `src/ordr/gate/` | Niranjan | Checks, hash, states, audit log, MCP server |
| `src/ordr/store/` | Niranjan (catalogue: Joel) | Dummy store and demo switches |
| `src/ordr/bot/` | Arya | Webhook, channels (Telegram, Meta), pipeline, outbox sender, LLM loop, voice |
| `src/ordr/matching/` | Arya | Item matching, household defaults |
| `src/ordr/contracts.py` | Niranjan and Arya | Shared types. **Both agree before changing** |
| Phones, accounts, manual tests | Joel | No code folder |
| PPT, video, interviews, submission | Farzin | No code folder |

## Run

```
python -m ordr.store.server      # store, port 8001
python -m ordr.gate.app          # gate, port 8002: MCP at /mcp and the /internal API
python -m ordr.bot               # bot: starts the channel in CHANNEL (telegram or meta)
```

The store and gate listen on this machine only. Only the bot's port 8003 (Meta) may be exposed, through a tunnel. **Never tunnel 8002.**

Run the gate as one process. Never use `--workers` or start it twice: the per-account locks live in its memory. The first start makes the demo household from `PARENT_NUMBER`, `KID_NUMBER` and the store's saved address, so start the store first. If you change either number in `.env`, the gate refuses to start: stop it and delete `ordr.db` (and `ordr.db-wal`, `ordr.db-shm` if present).

`.env` is shared by the three processes, but each reads only its own names. The bot never needs `SERVER_KEY`, and the code is written so it cannot read it. The bot refuses to start if a key its channel needs is empty.

## Bot setup

The bot needs `INTERNAL_TOKEN` (same value as the gate), `PARENT_NUMBER`, `KID_NUMBER`, `ANTHROPIC_API_KEY` (costs money: set a spending limit on the key) and the keys of its channel. `CHANNEL=telegram` is for testing and backup. `CHANNEL=meta` is the WhatsApp demo.

### Telegram (no public URL)

1. In Telegram, talk to `@BotFather`, send `/newbot`, copy the token into `TELEGRAM_BOT_TOKEN`.
2. Run `python -m ordr.bot --whoami`. From each phone, open the bot and send any message. It prints and replies with that chat's id. The parent's id is `PARENT_NUMBER`, the kid's is `KID_NUMBER`. Ctrl+C.
3. Set `CHANNEL=telegram`, start store, gate, then `python -m ordr.bot`. Both phones send "hi" first.

The bot uses long polling and private chats only. Buttons are inline buttons. Change a number later and you must delete `ordr.db` (see above).

### WhatsApp (Meta Cloud API)

1. In the Meta developer dashboard, make a Business app with the WhatsApp product. Use the test number. Add both demo phones as recipients.
2. Put these in `.env`: `META_TOKEN` (a system user token, not the 24 hour test token), `META_APP_SECRET` (App settings, Basic), `META_PHONE_ID` (the phone number ID, not the phone number), and `META_VERIFY_TOKEN` (any string you choose). Set `CHANNEL=meta`.
3. Start store, gate, then `python -m ordr.bot`. It listens on `127.0.0.1:8003` and refuses to start if a Meta key is empty.
4. Start a tunnel to the bot only: `cloudflared tunnel --url http://127.0.0.1:8003`. Copy the `https://....trycloudflare.com` address. It changes every restart.
5. In Meta, WhatsApp, Configuration, Webhook: Callback URL is `<tunnel address>/webhook`, Verify token is `META_VERIFY_TOKEN`. Click Verify and save. Subscribe to the `messages` field.
6. Both phones send "hi" to the test number first. Without a template, Meta only allows replies within 24 hours of the person's last message.

The webhook checks `X-Hub-Signature-256` on the raw body before reading it and answers 403 if it is wrong. With no `META_APP_SECRET` it answers 403 to everything.

### Watching it work

The bot appends every MCP call (tool name, milliseconds, ok) to `mcp_calls.jsonl`, never the arguments. Show it in a second terminal for the demo: `Get-Content mcp_calls.jsonl -Wait`. Logs hold no numbers, tokens or message text. Do not turn on debug logging for `httpx`: Telegram URLs contain the bot token.

## Rules

- One branch per person or task, small pull requests. `main` always passes tests.
- Before pushing: `ruff check . --fix`, `ruff format .`, `pytest`
- If a test in `test_contracts.py` fails, fix the code, not the test. Change it only if the team agreed to change the rule. The webhook test there checks that an unsigned POST gets 403.
- Never commit tokens, phone numbers or voice notes.
