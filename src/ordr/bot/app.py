"""Webhook. Text and voice from the parent go to the model. Buttons go to the gate.

Order of work for every request, no exceptions:
1. Channel.verify (provider signature). Fail: 403.
2. Drop message IDs already seen.
3. Drop unregistered numbers.
4. Button press: check role and mandate, then the gate. Never the model.
5. Kid's text: "log" or a fixed reply. Never the model.
6. Parent's text or voice: the model, with the five tools.
"""

from fastapi import FastAPI, HTTPException

app = FastAPI(title="Ordr webhook")


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/webhook")
def webhook() -> dict:
    # Refuses everything until signature checks exist.
    raise HTTPException(status_code=501, detail="Not built yet")
