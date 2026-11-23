# Working on Ordr

Read `CLAUDE.md` first. Using Antigravity or another AI tool? Tell it to read `CLAUDE.md` too.

## Setup (Windows, Python 3.11 or newer)

```
git clone https://github.com/Sky00raider/ordr.git
cd ordr
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
| `src/ordr/bot/` | Arya | Webhook, channel, buttons, LLM loop, voice |
| `src/ordr/matching/` | Arya | Item matching, household defaults |
| `src/ordr/contracts.py` | Niranjan and Arya | Shared types. **Both agree before changing** |
| Phones, accounts, manual tests | Joel | No code folder |
| PPT, video, interviews, submission | Farzin | No code folder |

## Run

```
python -m ordr.store.server      # store, port 8001
python -m ordr.gate.server       # gate, port 8002
uvicorn ordr.bot.app:app --port 8003
```

The store and gate listen on this machine only. Only port 8003 may be exposed (tunnel) for the webhook.

## Rules

- One branch per person or task, small pull requests. `main` always passes tests.
- Before pushing: `ruff check . --fix`, `ruff format .`, `pytest`
- If a test in `test_contracts.py` fails, fix the code, not the test. Change it only if the team agreed to change the rule. The webhook test goes away when the webhook is built.
- Never commit tokens, phone numbers or voice notes.
