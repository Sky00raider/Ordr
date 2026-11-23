# Working on CareGate

Read `CLAUDE.md` first. Using Antigravity or another AI tool? Tell it to read `CLAUDE.md` too.

## Setup (Windows, Python 3.11 or newer)

```
git clone https://github.com/Sky00raider/caregate.git
cd caregate
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
copy .env.example .env
pytest
```

## Who owns what

| Folder | Job | PRD |
|---|---|---|
| `src/caregate/gate/` | Checks, hash, states, audit log | Section 6 |
| `src/caregate/store/` | Dummy store and demo switches | Section 7 |
| `src/caregate/matching/` | Item matching, household defaults | Section 6A |
| `src/caregate/bot/` | Webhook, channel, buttons, voice | Sections 5, 7A |
| `src/caregate/contracts.py` | Shared types. **Agree before changing** | Sections 5, 6 |

## Run

```
python -m caregate.store.server      # store, port 8001
python -m caregate.gate.server       # gate, port 8002
uvicorn caregate.bot.app:app --port 8003
```

The store and gate listen on this machine only. Only port 8003 may be exposed (tunnel) for the webhook.

## Rules

- One branch per person or task, small pull requests. `main` always passes tests.
- Before pushing: `ruff check . --fix`, `ruff format .`, `pytest`
- If a test in `test_contracts.py` fails, fix the code, not the test. Change it only if the team agreed to change the rule. The webhook test goes away when the webhook is built.
- Never commit tokens, phone numbers or voice notes.
