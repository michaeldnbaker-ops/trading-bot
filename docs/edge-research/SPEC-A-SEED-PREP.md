# Spec A — active:false seed prep (FILED — DO NOT APPLY TO ENSEMBLE)

**Updated: 2026-09-23 — Edge Research**

**DO NOT APPLY. Do not seed into the ensemble. Do not write this row into live `agent_summary.json`. Do not activate. NO PROMOTE. No weight-test.**

This file is a **prep artifact only**. CoS / Learning Loop cleared **filing** it. A **separate** CoS greenlight is required before anyone copies the row onto the VM. Paper-only. No live. No strategy code.

Spec B **PARKED**. Spec C = **quality filter only**.

---

## Status

- Healthy EOD count: **5/5** (Learning Loop; Sep 16–18, 21–22)
- session_gates: **LANDED** (`fa605bd` / PR#10)
- Crypto residual: **clean** (Ops; broker_crypto NONE · ledger_crypto_opens 0)
- CoS / LL (2026-09-23): may **prepare / write** this seed **artifact** now
- **HOLD:** do **not** apply into live `agent_summary.json` / do not activate / **NO PROMOTE** / no weight-test until **separate** CoS greenlight
- B **PARKED**; C quality-filter only; paper-only

**This document does not seed the ensemble.** Presence of the JSON below in git is not a roster row. Missing-from-summary is not a promote. Applying it without the separate CoS seed-write greenlight is out of scope.

---

## Exact seed row (prep artifact — apply only after CoS seed-write greenlight)

**DO NOT APPLY** until that greenlight. When (and only when) CoS clears the write, add (or ensure) this entry in `agent_summary.json` (path as used by Learning Loop / Ops on VM):

```json
{
  "RegimeEquityAgent": {
    "active": false,
    "note": "Spec A RegimeEquity — cold seed per ROTATION-CONTRACT; PROMOTE only when Technical BENCHED (A first) or OptionsFlow BENCHED (A only)"
  }
}
```

If the live schema uses a list of agent rows instead of a map, equivalent shape:

```json
{
  "agent": "RegimeEquityAgent",
  "active": false,
  "weight": null,
  "status": "seeded"
}
```

Ops/LL: match whatever schema `agent_summary.json` already uses — **`active` must be false**; missing row ≠ promote. **`active: true` is forbidden** on this seed. Do not set a weight. Do not PROMOTE in the same change.

---

## AGENT_VARIANTS (confirm on apply; do not change without CoS)

- TechnicalAgent → RegimeEquity (Spec A) **first**
- OptionsFlowAgent → RegimeEquity (Spec A) **only**
- Spec B **not** listed for PROMOTE

Confirm on apply. Do not edit rotator code or `AGENT_VARIANTS` from this prep file.

---

## Checklist

- [x] session_gates live on VM
- [x] crypto residual clean
- [x] validation envelope + kill numerics ([SPEC-A-VALIDATION.md](SPEC-A-VALIDATION.md))
- [x] this seed prep artifact filed
- [ ] CoS **apply** greenlight (separate) — **HOLD / DO NOT APPLY**
- [ ] Write row into live `agent_summary.json` on VM — **blocked; do not seed into the ensemble**
- [ ] Hand Learning Loop confirmation
- [ ] PROMOTE only after parent BENCHED — **NO PROMOTE**
- [ ] Report CoS; paper-only; no live

---

## Out of scope for this artifact

- Editing live or repo `agent_summary.json`
- Ensemble activation, weight-test, or PROMOTE
- Spec B implementation (PARKED) or treating Spec C as alpha
- Live brokerage, crypto, strategy code
