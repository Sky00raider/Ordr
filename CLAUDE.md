# CareGate (wHACKiest 2026, Track 1: Make MCP Matter)

Full spec: `docs/CareGate-PRD-v1.4.md` (now v1.4.2). If this file and the PRD differ, follow the PRD and flag it.

**Phase:** idea locked, building the demo. Stack: Python 3.11+, FastMCP, FastAPI, SQLite. Scaffold and contract tests exist in `src/caregate/`; features are stubs. Setup: `CONTRIBUTING.md`.

## Team and style
- Niranjan (Claude Code), Arya, Farzin, Joel (Antigravity). CSE AI&ML, 3rd year, MSRIT Bangalore.
- Output: dense, direct, plain markdown, **no em dashes**, metric units. Short sentences, no hype.
- Build something genuinely useful. Do not optimise for sponsor prizes.

## Deadlines
| When | What |
|---|---|
| **24 Nov 2026, 11:59 PM IST** | Devnovate submission: PPT (strict official template, no extra slides, named `TeamName_TrackNumber`, public Drive link), 3 to 5 min YouTube video with prototype walkthrough, public GitHub repo with MVP. Same repo until presentations. |
| 26 to 27 Nov | Online pitch: 5 min plus 3 min Q&A. Judged on problem understanding, reasoning, clarity, early progress |
| 1 to 2 Dec | 24 h offline build |

## What we are building
A parent sends a WhatsApp voice note in Kannada or English. The bot turns it into exact store items and reads the order back. The parent taps Confirm. The kid gets a card and taps Approve and pay, then pays by UPI (simulated). CareGate places an order only if it is exactly the one approved.

**Why it exists:** Swiggy Instamart's MCP `checkout` takes no items. It buys whatever is in the server-side cart, and there is no cancel tool.

**Why MCP:** stores already publish MCP servers. Our MCP server sits in between and gives the AI five safe tools and no checkout. "It can shop, but it can't buy."

**Flow:** voice note > item matching (ask if unclear) > cart built and read back > parent Confirm > kid's card (Approve and pay / Open in store / Reject) > re-check live cart at once > `checkout` > re-read order > UPI PIN (Pay / Don't pay) > receipt. No hold. Parent can Stop until the kid approves.

## Decisions (do not reopen)
- WhatsApp is the only channel for both people. No web pages. No Claude Desktop or second client.
- The WhatsApp bot is the MCP client.
- Store account is the kid's, who pays. Delivery to the parent's fixed saved address.
- Demo store is a dummy Swiggy Instamart: Swiggy's documented tool names, made-up products. Label it "Demo store modelled on Swiggy Instamart's published MCP tools. Not affiliated with Swiggy. No real orders. Live access pending." No Swiggy logo, colours or badge.
- Hoped-for vendors: Swiggy Instamart, Zepto, ONDC. Access is pending.
- Groceries and essentials only. No prescription medicines.
- Safe-phrase kept on kid's messages. Two caps, fixed at setup: Rs 1,500 per order, Rs 6,000 per month (placeholders).
- Dropped: Flatmate Gate. Roadmap: Razorpay, live vendors, cross-store checks.

## Rules the code must keep
1. The model never supplies a fact the gate checks. Cart, total, address and household come from the server.
2. Button presses go straight from the webhook to CareGate, never through the model. Text or voice "yes" does not count.
3. The read-back, cards, payment and status messages are written by code from store data. The model writes only clarifying questions to the parent.
4. Assistant tools are only `search_products`, `prepare_order`, `get_order_status`, `abort_order`, `get_audit_log`. No `address_id`, total or approval as input.
5. Item IDs must come from a store search for this household in the last 15 minutes. Never guess or silently swap.
6. A press counts only from the registered number and for the current `mandate_id`. Verify the webhook signature and ignore repeated message IDs. Check household ownership and role on every tool call and press.
7. Never replace the kid's existing cart without their tap.
8. Right after approval, re-read the cart and check state, signature, hash, total, stock, caps. Any failure means zero order calls. Every state change is one atomic update.
9. Re-read the order before showing payment. Never retry `checkout` blindly; check `get_orders`. Never set `confirmDuplicate` automatically. Unknown or mismatched result is `NEEDS_REVIEW`: no payment message, account blocked.
10. Price ceiling is quoted total plus Rs 30 (demo: Rs 345, ceiling Rs 375). Prices and fees stay out of the cart hash.
11. No tokens, phone numbers or family voice notes in the repo.

**Build order:** dummy store and gate with tests > WhatsApp buttons with typed orders > item matching > voice in and out > defaults, duplicate warning, Open in store. Required tests are in PRD section 8.

## Claims
- **Say:** "we found no...", "inspired by AP2 Cart Mandates", "tamper-evident", "the approval pattern is known; the new part is a family and a real grocery flow".
- **Never say:** first, only, proves, immutable, real orders, live Swiggy, fully automatic, stops scam calls, voice or WhatsApp ordering is new, any accuracy we have not measured.
- Say "applied to Swiggy" only after applying.
- RBI "trusted person" (Apr 2026 paper, publication 23810) is a proposal above Rs 50,000; merchant payments can be exempted.
- Evidence table and banned stats: PRD section 2. Competitors: PRD section 10.

## Known unknowns (full list: PRD section 12)
- WhatsApp provider: 90 min total: Meta's test number, then Twilio sandbox, then Telegram (and say so). Max 3 buttons, 20 characters each. Without a template, the person must have messaged in the last 24 h, so both phones send "hi" first.
- Kannada speech-to-text and text-to-speech not picked or tested.
- Whether an MCP-filled cart shows in the Swiggy app. "Open in store" depends on it.
- Final product name (CareGate, Anumati, Nambike, Nod) and team name.
- Ask 3 families. Ask CodeRIT whether pre-written code is allowed in the 24 h build.

## Docs
| File | What |
|---|---|
| `docs/CareGate-PRD-v1.4.md` | Current spec |
| `README.md` | Public summary for judges |
| Local only, not in git: `docs/research/elder-research.md`, `elder-deep-research.md` | Problem evidence, voice, privacy law |
| Local only: `docs/research/vendor-mcp-access.md` | Swiggy, Zepto, Zomato, ONDC access |
| Local only: `docs/research/prior-art.md`, `borrowed-mechanisms.md` | Similar projects, patterns to borrow and credit |
| Local only: `docs/research/track-landscape.md` | Early all-track report with sources |
| Local only, not in git: `docs/archive/old-CLAUDE-ideation.md` | Old ideas incl. the UPI shop map. Not current; do not follow |
