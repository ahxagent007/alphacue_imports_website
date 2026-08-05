# AlphaCue Imports — Business & Finance Module

Development plan for the owner-facing business management system:
product costing, client invoicing, dues & payments, cash tracking,
investor capital, and loans.

**Decisions locked in:**

| Decision | Choice |
|---|---|
| Accounting engine | Hybrid ledger — balanced entries, all balances derived, plain-language UI |
| Invoice recipients | Wholesale/B2B buyers, website retail orders, walk-in/offline sales |
| Product costing | Mixed — China import consignments (RMB) **and** simple local purchases |
| Costing entry | Two-stage: RMB costs at order, weight charge on arrival |
| Per-kg weight rate | Per product line, defaulting from the shipment |
| Weight reconciliation | Agent's billed weight entered; line weights scaled to match it |
| Shipment `extra_cost` | BD-side costs (local shipping, repacking) allocated **by weight** |
| Correction % | Applied to the full subtotal — goods + freight + extra cost |
| Stock valuation | **FIFO** — oldest batch consumed first, per-shipment cost retained |
| Stock tracking | Full movement ledger with batch consumption records |
| Location | New `finance` app, staff panel at `/manage/finance/` |

**Assumptions made** (flag if wrong):
- Affiliate resellers are **not** invoiced through this system — they stay on the
  existing commission/`ResellerPricing` flow. Their withdrawals still post to the
  ledger (M10) so payouts appear in the cash position.
- Single owner-operator plus optional staff. No approval workflows or maker/checker.
- No VAT/BIN tax reporting requirement. If VAT invoicing is legally needed, that adds
  scope to M3.

---

## Architectural core

Everything rests on three tables. Build these right and the other six features are
mostly views and forms over them.

```
Account
  ├ code, name
  ├ type: cash | bank | mobile_money | receivable | payable
  │       | inventory | equity | loan_payable | loan_receivable
  │       | income | expense
  ├ party      → optional FK (client, supplier, investor, lender)
  ├ is_active, opening_balance, opening_date
  └ (balance is NOT stored — always computed)

Transaction
  ├ date, reference_no, description
  ├ source_type / source_id   → what created it (Invoice, Payment, Loan, …)
  ├ created_by, created_at
  └ is_reversed, reversal_of  → corrections are reversal entries, never edits

TransactionLine
  ├ transaction  → FK
  ├ account      → FK
  ├ amount       → signed Decimal; SUM per transaction must equal 0
  └ memo
```

**Derived balances — the whole point:**

| Question the owner asks | How it's answered |
|---|---|
| How much cash do I have? | `SUM(lines.amount)` where `account.type in (cash, bank, mobile_money)` |
| What does Rahim Traders owe me? | `SUM(lines.amount)` where `account = AR:Rahim Traders` |
| What have I invested vs my partner? | Balance of each `equity:Investor` account |
| How much is left on the bank loan? | Balance of `loan_payable:BankZ` |
| Profit this month? | Income accounts − expense accounts, date-filtered |

Nothing is stored twice, so nothing can disagree with itself.

**Non-negotiable invariants** (enforced in `finance/services.py`, covered by tests):
1. A transaction is never saved unless its lines sum to exactly zero.
2. A posted transaction is immutable. Mistakes are fixed by posting a reversal.
3. All posting goes through `post_transaction()` inside `transaction.atomic()`.
   No view or admin action ever writes a `TransactionLine` directly.
4. All money is `Decimal`, never float. Rounding to 2 places at the boundary only.

---

## Milestones

Milestones are sequential — each builds on the last. M1 must be solid before
anything else; it is the foundation everything else derives from.

### M0 — Groundwork  ✅ DONE
*Prep work before any finance code.*

- **Fix `settings.py:89–109`** — `DATABASES` is defined twice and the SQLite block
  overwrites the MySQL one. Right now the app runs on SQLite regardless of `.env`.
  Must be resolved before real money is recorded.
- Create the `finance` app; register in `INSTALLED_APPS`.
- Staff-only access mixin/decorator; wire `/manage/finance/` URL namespace.
- Base templates matching the existing `/manage/` staff panel styling.
- Confirm a working DB backup routine on cPanel.

**Ships:** nothing user-visible. Safe base to build on.

---

### M1 — Ledger core  ✅ DONE
*The engine. No UI beyond debugging screens.*

**Design choices made during build that differ from the sketch above:**

1. `Transaction.reference_no` is **derived from the pk** (`TXN-000042`), not a
   stored column. A stored value needs a second write and can drift from the row
   it names; a property cannot.
2. **Opening balances are transactions, not fields.** The `opening_balance` /
   `opening_date` columns in the original sketch were dropped. M2 will post them
   against account `3900 Opening Balances` instead, so the "every balance is
   derived" rule holds with no exceptions.
3. **Known limitation:** the immutability guards live in `save()`/`delete()`, so
   they catch object-level changes but not bulk queryset operations
   (`Transaction.objects.filter(...).delete()`), which Django routes around the
   model layer. The trial balance is the backstop — it will show a non-zero
   total if anything ever bypasses the service.

- `Account`, `Transaction`, `TransactionLine` models + migrations.
- `post_transaction(date, description, lines, source=None)` — atomic, validates
  balance to zero, rejects inactive accounts.
- `reverse_transaction(txn, reason)` — the only correction path.
- Balance query helpers: `account_balance(account, as_of=None)`,
  `type_balance(types, date_range)`, `party_balance(party)`.
- Chart-of-accounts seed command with sensible defaults for the business.
- Register in Django admin (read-only) for inspection.
- **Test suite**: balanced-entry enforcement, reversal correctness, balance math
  across date ranges, concurrent posting safety.

**Ships:** an auditable ledger. Nothing to click yet, but everything after this is
fast because the hard part is done.

---

### M2 — Cash, accounts & expenses  ✅ DONE
*First screens the owner actually uses.*

- Account management UI — create cash drawer, bank accounts, bKash, Nagad.
- Opening balance entry per account with an opening date.
- Expense recording with categories (rent, salary, courier, marketing, packaging,
  utilities, misc — editable).
- Money transfer between own accounts.
- **Cash book / day book** — chronological entries with a running balance, filterable
  by account and date range.
- Balance summary strip: total cash across all accounts, per-account breakdown.

**Ships:** the owner can track every taka in and out, and always know the cash
position. Immediately useful on its own.

**Design choices made during build:**

1. **Expense categories are expense accounts**, not a separate model. Adding a
   category means adding a 5000-series account, which means new categories show
   up in the P&L automatically with no extra wiring.
2. **Transfers carry an optional charge.** bKash/Nagad cash-out fees are taken
   from the source account *on top of* the transferred amount and posted to
   `5160 Bank & Payment Charges` — so a ৳5,000 cash-out with a ৳93 fee moves
   ৳5,093 out of bKash, ৳5,000 into cash, and ৳93 into expenses.
3. **Opening balances are entered as positive numbers** whatever the account
   type; the sign is derived from `Account.normal_sign`. Cash you hold and money
   you owe are both typed in as positives.
4. **One opening balance per account**, enforced in the service. Changing it
   means reversing the original first, which keeps the correction visible.
5. **Reversal is exposed in the UI** on the transaction detail page, and requires
   a written reason that is recorded on the reversal's description.

---

### M3 — Parties & invoicing  ✅ DONE
*The invoice generator.*

- `Party` model — type: `wholesale_client` | `retail_customer` | `walkin` | `supplier`.
  Name, phone, email, address, credit limit, notes.
- Auto-create a receivable account per credit client on first invoice.
- `Invoice` + `InvoiceItem` — number series (`INV-2026-0001`), issue date, due date,
  payment terms, line items, per-line discount, order-level discount, delivery charge,
  notes/terms footer.
- Three creation paths:
  1. **From a website `Order`** — one click, prefills customer, items, delivery fee.
  2. **New wholesale invoice** — pick client, add lines from `ProductVariant` catalogue.
  3. **Walk-in quick invoice** — minimal form, no stored client required.
- Status lifecycle: `draft → sent → partially_paid → paid`, plus `overdue` (derived
  from due date) and `cancelled`.
- Issuing an invoice posts to the ledger: receivable up, sales income up.
- **PDF generation** — branded layout with logo, business details from `SiteSettings`,
  bKash/Nagad/bank payment instructions.
- Public share link (signed, expiring token) so a client can view/download without login.

**Ships:** professional invoices, generated in seconds, from any sales channel.

---

### M4 — Dues & payments  ✅ DONE
*Getting paid, and knowing who hasn't paid.*

- `Payment` model — client, date, amount, method (cash/bKash/Nagad/bank), reference.
- Allocation of one payment across multiple invoices; handles partial payments,
  overpayments (credit balance), and advances received before invoicing.
- **Client ledger statement** — every invoice and payment for one client, running
  balance, printable/PDF.
- **Aging report** — receivables bucketed 0–30 / 31–60 / 61–90 / 90+ days.
- Receivables dashboard: total outstanding, overdue count and value, worst offenders.
- Credit limit warning when invoicing a client already over their limit.
- Supplier payables mirror the same machinery (`Bill` + `PaymentMade`).

**Ships:** complete dues management. Combined with M2–M3, this is a working
business system even if development paused here.

---

### M5 — Purchasing & landed costing  ✅ DONE
*True landed cost, and therefore true margin.*

The China import flow is **two-stage**, because the weight charge doesn't exist until
the shipment lands and the agent weighs it. Cost is *provisional* after stage 1 and
*final* after stage 2. The model holds both states rather than pretending cost is
known at order time.

**Models:**

```
Purchase                       (one China shipment, or one local purchase)
  ├ purchase_no                 PUR-110
  ├ type: import | local
  ├ supplier, purchase_date
  ├ fx_rate_rmb_to_bdt          ¥1 = ৳X          (import only)
  ├ default_per_kg_charge_bdt                     (import only)
  ├ billed_weight_kg            agent's billed total, entered on arrival
  ├ extra_cost_bdt              BD shipping, repacking, clearing, misc
  ├ correction_percent          e.g. 5.00
  ├ status: draft → ordered → in_transit → received
  └ received_date

PurchaseItem                   (one row per ProductVariant)
  ├ variant → store.ProductVariant
  ├ quantity
  ├ unit_price_rmb                          ← stage 1
  ├ domestic_shipping_rmb    China-side, supplier → forwarder   ← stage 1
  ├ entered_weight_kg                       ← stage 2
  ├ per_kg_charge_bdt        defaults from Purchase, overridable ← stage 2
  └ computed cost snapshot (all fields below, frozen on receipt)
```

**The calculation:**

```
Stage 1 — at order, RMB known:
  goods_rmb      = unit_price_rmb × quantity
  line_rmb       = goods_rmb + domestic_shipping_rmb
  goods_bdt      = line_rmb × fx_rate

Stage 2 — on arrival, weight known:
  scale          = billed_weight_kg / SUM(entered_weight_kg)
  line_weight    = entered_weight_kg × scale      ← reconciles to the agent's bill
  freight_bdt    = line_weight × per_kg_charge_bdt
  extra_alloc    = extra_cost_bdt × (line_weight / billed_weight_kg)

  subtotal_bdt   = goods_bdt + freight_bdt + extra_alloc
  landed_total   = subtotal_bdt × (1 + correction_percent / 100)
  landed_unit    = landed_total / quantity
```

**Worked example** — two products, FX ¥1 = ৳17.50, ৳100/kg, extra cost ৳500,
correction 5%. Entered weights total 10kg but the agent billed 11.5kg:

| | Product A | Product B |
|---|---|---|
| Quantity | 10 pcs | 20 pcs |
| Unit price | ¥50 | ¥15 |
| Domestic shipping | ¥30 | ¥20 |
| Goods in RMB | ¥530 | ¥320 |
| **Goods in BDT** | **৳9,275.00** | **৳5,600.00** |
| Entered weight | 6 kg | 4 kg |
| Scaled weight (×1.15) | 6.9 kg | 4.6 kg |
| Freight @ ৳100/kg | ৳690.00 | ৳460.00 |
| Extra cost share (by weight) | ৳300.00 | ৳200.00 |
| Subtotal | ৳10,265.00 | ৳6,260.00 |
| **+ 5% correction** | **৳10,778.25** | **৳6,573.00** |
| **Landed cost per piece** | **৳1,077.83** | **৳328.65** |

Freight totals ৳1,150 = 11.5kg × ৳100 — exactly the agent's bill, fully allocated.

**Rounding rule (important):** the **line total** is authoritative; per-unit cost is
derived for display. Allocation remainders go to the largest line so allocated amounts
sum exactly to the shipment total. Without this, every purchase leaves small
unexplained gaps and the ledger stops balancing.

**Local purchase flow:**
- No RMB, no FX, no weight. Supplier, variant, quantity, unit cost in BDT, optional
  transport cost, correction %. Single-stage — entered once, already received.

**Both flows:**
- **Margin report** — landed cost vs `ProductVariant.price`, showing margin taka and
  margin %, sortable to surface loss-making products.
- Purchases post to the ledger: goods-in-transit while shipping, converting to
  inventory on receipt; supplier payable up.
- Receiving a purchase creates stock batches (M6).

**Ships:** the owner finally knows what each product actually costs and which ones
make money.

---

### M6 — Stock, batches & FIFO  ✅ DONE
*Stock that can always explain itself, and cost that follows each unit to the sale.*

FIFO means each shipment's cost stays attached to its units until they're sold. That
needs batches, not just a counter.

**Models:**

```
StockBatch          created when a PurchaseItem is received
  ├ variant, purchase_item
  ├ unit_cost         the landed cost from M5
  ├ qty_received, qty_remaining
  └ received_date     ← FIFO ordering key

StockMovement       one row per change, ever
  ├ variant, date, quantity (signed), reason, reference
  └ reason: purchase_receipt | sale | sale_return
            | damage | adjustment | opening

StockConsumption    which batches an outbound movement drew from
  ├ movement, batch, quantity, unit_cost
  └ this is what makes FIFO auditable rather than merely computed
```

**Work:**
- `consume_stock(variant, qty, reason, reference)` — oldest batch first, spans
  multiple batches, `select_for_update()`, fully atomic. The single path for all
  outbound stock.
- `receive_stock(purchase_item)` — creates the batch and the inbound movement.
- `ProductVariant.stock` becomes a cached sum of movements, with a
  `reconcile_stock` management command to detect and repair drift.
- **Replace `store/checkout_views.py:149-151`** — the current
  `max(0, stock - quantity)` silently clamps at zero and hides oversells. Becomes a
  real guard plus a `consume_stock()` call.
- **Per-product cost history** — the shipment × cost table:

```
Product: USB-C Cable 2m
Shipment   Date         Qty    Landed cost/pc   Remaining
─────────────────────────────────────────────────────────
PUR-110    2026-03-14   100         ৳10.00           0
PUR-112    2026-05-02   150         ৳12.00          30
PUR-119    2026-07-21   200         ৳11.40         150
```

- **Stock ledger page** per product — every movement, dated, with reason and running
  balance.
- Reorder level per variant + low-stock alerts on the dashboard.
- **COGS posting** — each sale posts inventory → cost-of-goods-sold at the *actual*
  batch costs consumed, so reported profit uses real numbers.
- Returns restore quantity to the originating batch where known.
- **Tests**: FIFO across batch boundaries, partial consumption, a sale spanning three
  batches, returns, concurrent sales of the last unit, reconciliation correctness.

**Ships:** trustworthy stock, per-shipment cost history, and accurate per-sale profit.

---

### M7 — Investors  ✅ DONE
- `Investor` model with a dedicated equity account each.
- Capital injection and capital withdrawal/drawing entries.
- Ownership % — computed from capital contributed, or manually overridden.
- **Profit distribution run** — pick a period, system computes net profit from the
  ledger, splits by ownership %, records each investor's share (paid out or retained).
- Per-investor statement: capital in, drawings, profit share, current standing.
  Printable/PDF for sharing with the investor.

**Ships:** clean investor reporting with no spreadsheets.

---

### M8 — Loans  ✅ DONE
- `Loan` model — direction (taken / given), lender or borrower, principal, interest
  rate, method (flat or reducing balance), tenure, start date.
- **Installment schedule generation** — due dates with principal/interest split per
  installment.
- Payment recording against the schedule; each payment splits correctly between
  principal, interest expense, and cash.
- Outstanding principal, interest paid to date, remaining installments.
- Overdue installment alerts on the dashboard.
- Both directions post to the ledger (`loan_payable` / `loan_receivable`).

**Ships:** every borrowing and lending obligation visible and current.

---

### M9 — Owner dashboard & reports  ✅ DONE
*Everything above, answered at a glance.*

- **KPI dashboard**: cash on hand (per account and total), receivables outstanding,
  payables outstanding, this month's revenue / expense / net profit, loan
  installments due this week, overdue invoice count.
- **Profit & Loss** for any period, income and expense broken down by category.
- **Cash flow summary** — opening balance, in, out, closing, by month.
- Trend charts: monthly revenue vs expense, cash balance over time.
- CSV export on every report.

**Ships:** the "how is my business doing?" screen.

---

### M10 — Integration & hardening  ✅ DONE

**Design choices made during build:**

1. **Hooks report, they never raise.** The store and affiliate apps predate the
   ledger. Every hook returns a `HookResult` and the caller shows a warning —
   a missing chart of accounts must not stop someone approving a commission.
2. **Hooks are idempotent.** Posting is skipped when a live entry already
   exists for that source object, so a double-clicked button cannot book the
   same money twice.
3. **Commissions post on approval, not on creation.** A pending commission may
   still be rejected as fraud; booking it would overstate what is owed.
4. **Withdrawal ids are offset by 1,000,000** inside the shared `affiliate`
   source type so they cannot collide with commission ids.
5. **Auto-invoicing is off by default.** It creates a numbered document for
   every retail sale, which most shops do not want.
6. **The audit log reads the ledger**, not a separate audit table — a second
   record would only be another thing that could disagree with the first.
- **Affiliate integration** — approved `WithdrawalRequest` payouts post to the ledger
  so commission payouts appear in cash flow and expenses. Accrued commission liability
  visible alongside other payables.
- **Order integration** — configurable: when an `Order` hits `delivered`, optionally
  auto-generate an invoice or post revenue directly.
- Audit log view — who created/reversed what, when.
- Role permissions if staff beyond the owner get access.
- MySQL migration verification (the M0 fix, validated with real data volume).
- Backup/restore procedure documented in `DEPLOY.md`.
- Production deploy.

**Ships:** one connected system rather than two apps sharing a database.

---

## Sequencing notes

- **M1 is the gate.** Rushing it and patching later means re-deriving every balance
  in the system. Budget real time for its tests.
- **M2 → M3 → M4 is the highest-value run.** After M4 the owner has invoicing, dues,
  and cash tracking — genuinely usable, independent of the rest.
- **M5 + M6 are a pair.** M6 depends on M5's landed cost, and M5 isn't fully useful
  until M6 puts those costs against stock. Plan them together. This pair can move
  ahead of M3/M4 if knowing true product margin is more urgent than invoicing.
- **FIFO makes M6 the heaviest milestone after M1.** Batch consumption, concurrency
  on the last unit, and returns all need real test coverage. If schedule pressure
  hits, the fallback is weighted-average costing — but that decision must be made
  *before* M6 starts, not during.
- **M7 and M8 are independent of each other** — order them by whichever is more
  pressing.
- **M9 is cheap** if M1 was done properly; it is mostly queries over existing data.

## Open items to decide

**Before M3:**
1. VAT/BIN on invoices — required, or not applicable?
2. Invoice number format, and whether the series resets yearly.

**Before M5:**
3. Is `correction_percent` set per shipment, or a global default that can be
   overridden per shipment?
4. Are supplier payments made per shipment, or as a running account with the supplier?

**Before M6:**
5. Sale returns — restore to the original batch (needs the sale to remember which
   batch it consumed, which `StockConsumption` gives us), or into a fresh batch at
   current cost?
6. Negative stock — block the sale outright, or allow it and flag for correction?
7. Damaged/lost goods — write off to an expense account, or absorb into the
   correction %?
