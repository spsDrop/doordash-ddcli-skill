# DoorDash Ordering Skill (dd-cli)

This skill wraps the **official DoorDash CLI** (`dd-cli`, on `$PATH`) and
ships a Python helper, **`ddtools`**, on top of it. **Prefer `ddtools`** for
the two fiddly-by-hand jobs it covers — subset reorders and option trees; it
does the retry / cleanup / verify bookkeeping. Using these methods greatly increase speed and success of attempting to assemble orders and reorder. The raw `dd-cli` commands are
what it builds on and cover everything else.

## What this skill provides (front-line tools)

`scripts/ddtools.py` — one Python entrypoint wrapping `dd-cli` (runs it from
`$PATH`, auth via `DD_CLI_ACCESS_TOKEN`; every subcommand prints clean JSON).
**Reach for these first** when the job fits:

```
python3 scripts/ddtools.py reorder <order_uuid> [indices…] [--cleanup] [--json]
    Re-order 1-based receipt-line indices (default = all) into a fresh cart:
    nukes pre-existing open carts, adds lines with their receipt options
    (bounded retries: transient → 15s re-add of just those items; rejected
    options → retry bare; ITEM_UNAVAILABLE → terminal, no retry), verifies the
    cart line-by-line, runs preview. Leaves the cart OPEN for `order submit`
    unless --cleanup (dry-run: deletes the verified cart, including on FAIL —
    never leaks an open cart). PASS = cart matches the re-ordered lines
    exactly. `asap_available:false` (store closed / scheduled-ahead, e.g. at
    night) does NOT fail a reorder.

python3 scripts/ddtools.py read-options --store-id S (--item-id I | --query NAME) [--menu-id M]
    Flatten the item's option tree into rows {kind,path,top,parent,depth,id,
    name,price,is_default,enum}. kind=group (min/max/required), kind=option
    (pickable leaf), kind=choice (expander w/ sub-groups). Pass `parent` when
    an option id could collide across branches.

python3 scripts/ddtools.py write-options --store-id S --item-id I --picks '[{"id","parent"?,"name"?,"quantity"?}]' [--quantity N] [--keep-defaults]
    Resolve flat picks (from read-options) back into a valid `nested_options`
    items-json entry for `cart add-items` (default_handling:exact;
    --keep-defaults omits it). Emits `items_json` ready to paste.

python3 scripts/ddtools.py carts [--delete-all]
    List open carts (all stores); --delete-all empties every open cart.
```

**Known environmental (not tool) failures** — expect these at night or for
old orders; the JSON result carries `"env":true` when it's the menu case:
- store has no menu right now (`MENU_NOT_FOUND`) → closed/renumbered/stale id.
- `ITEM_UNAVAILABLE` ("store confirmed these items are not orderable right
  now") → store-side stock/hours; retry when open, don't keep retrying.
- `ITEM_MUTATION_UNAVAILABLE` / `CART_MUTATION_UNAVAILABLE` (transient) →
  ddtools already sleeps 15 s and re-adds just the failing items.


## What the base dd-cli covers

Everything not handled by `ddtools` runs straight through `dd-cli`. The core
restaurant flow (search → menu → item-details → cart → preview → submit), plus
grocery/retail, order history/receipt/reorder, promos, addresses, and payment
methods are detailed below — this is the general-purpose surface.

## Setup

1. **Install the binary** (once): `bash scripts/install-dd-cli.sh` — downloads
   the official release from `doordash-oss/doordash-cli`, verifies SHA256,
   installs to `~/.local/bin/dd-cli`.
2. **Authenticate**: `dd-cli login` opens a browser and saves your session to
   the OS keychain. For a headless host, run `dd-cli export-token` on a
   machine with a browser and set the printed value as `DD_CLI_ACCESS_TOKEN`
   in the headless environment (see `scripts/token-fetch.sh`).
3. `dd-cli --version` should print `dd-cli, version 0.2.x`.

## Run conventions

- `dd-cli <command> …` prints clean JSON to stdout **always**. If a call
  fails, stdout starts with `Error: ` followed by JSON — strip the prefix
  before `json.loads`:
  `s.split('Error: ', 1)[1] if s.startswith('Error:') else s`.
- **Every command needs `--intent`** (even `cart list`, `remove-item`,
  `preview`, `submit`) or it is rejected. **Keep it generic and banal; never
  put addresses, names, payment, or other personal data in it** (it may be
  harvested). e.g. `--intent "ordering food for me"`.
- Auth is the `DD_CLI_ACCESS_TOKEN` env var (or the keychain). Run `dd-cli`
  bare — do not re-source your shell profile per call.
- **`order submit` CHARGES THE CARD.** Only call it after the user explicitly
  confirms items, total, and tip. Use `-y` to skip the interactive prompt.

## Core flow (restaurant)

```
dd-cli search -q "togo" --limit 5                     # stores[].store_id
dd-cli menu --store-id 160                            # menu_id + items[] (item_id has `i_` prefix)
dd-cli restaurant-item-details --store-id 160 --menu-id 160 --item-id i_29789962845
     # → item.extras[]: title, min/max options, options[].option_id (o_ prefix)
dd-cli cart list --store-id 160                       # check for an existing cart FIRST
dd-cli cart add-items --store-id 160 --menu-id 160 --intent "order food" \
     --items-json '[{"item_id":"i_29789962845","item_name":"...","quantity":1,
                     "nested_options":[{"id":"o_45949004445","name":"No Onions","quantity":1}]}]'
dd-cli cart list --store-id 160                       # source of truth: lines + option names + cart_item ids
dd-cli order preview --cart-uuid <cart>               # real price, no charge
dd-cli order submit --cart-uuid <cart> --tip-cents 300 -y
```

State is the **`cart_uuid`** you carry between calls (no hidden session).
`--store-id`/`--menu-id` are the same number for most stores (menu id = store
id). `add-items` is **additive** (see Cart state below).

### New in v0.2.5 (verified live 2026-09-23)
- **search facets** (optional): `--dashpass-only`, `--price-tier N`
  (repeatable, 1=cheapest), `--distance-preference nearby|balanced|broad`,
  `--max-eta-minutes N`.
- **search results carry pickup signals per store**: `offers_pickup`,
  `asap_pickup_availability`, `scheduled_pickup_availability`,
  `next_open_time_asap_pickup_ms`.
- **weight-priced items** work in `cart add-items` (meat/deli/produce by the
  pound).
- **merchant default mods** apply automatically; pass
  `default_handling:"exact"` on an item in `add-items` to opt out.
- **order-ahead**: `search`/`menu`/`cart add-items` surface schedule-ahead
  fields; you can schedule an order ahead of time.

## Options / modifiers (the reason to prefer this skill)

Customizations go in `nested_options[]` inside each `--items-json` entry.
**Flat form works and is simplest**: one object per leaf option, id + name +
quantity — it spans all option groups (required + optional) at once:

```json
[{"item_id":"i_29789962845","item_name":"The Cheesesteak (Shorty)","quantity":1,
   "nested_options":[
     {"id":"o_45949004445","name":"No Onions","quantity":1},
     {"id":"o_45949004450","name":"Hot Cherry Peppers","quantity":1}]}]
```

- Prefixes `i_`/`o_`/`e_` are **accepted in input but stripped on the wire** —
  echoed ids come back bare (`45949004445`). Match by suffix when comparing.
- Get option ids from `restaurant-item-details` (per item) or the error
  response. `menu` items do **not** include options — use item-details.
- **Required option missing** → `add-items` returns `success:false` with
  `item_errors[].required_options[]` listing valid choices (id+name).
- **Transient `ITEM_MUTATION_UNAVAILABLE`** ("service could not be reached")
  with **no** `required_options` → DoorDash flakiness: wait ~15 s, retry that
  item alone. Do NOT replay a whole batch (quantities merge, you'd duplicate).
- An item added bare can never be retrofitted with options afterwards as a
  plain option-only add (required options block it) — **add the full option
  set in one call**.

## Cart state (verified behavior)

- `add-items` merges by **(item + exact option set)**: same combo → qty +=; a
  *different* option set → a **new line**. It returns the **existing
  cart_uuid** for the store (doesn't create a new one) and is additive.
- To change an item's options: add the new-variant line, then
  `cart remove-item --cart-uuid <c> --cart-item-id <line-id> --intent …`.
- `remove-item` takes **`--cart-item-id`** (the line's `id` from `cart list`,
  **not** `--item-id`) and has **no `--quantity` flag** — it removes the whole
  line. Adjust qty = remove + add at the right quantity (or delta-add).
- **`cart list --store-id <id>` is the source of truth** and the only cheap
  place that shows option *names* (`cart show`'s nested_options have empty
  names). Check it before mutating, and always after.
- `cart delete --cart-uuid <c>` empties it (next add mints a fresh cart).
- **DoorDash silently deletes carts server-side** (e.g. store closed) → later
  calls fail with `CART_NOT_FOUND`. Re-add items; a new cart is fine.
- **A cart can become un-submittable while still looking healthy**: `order
  preview` keeps succeeding, but `order submit` returns `CART_SUBMIT_REJECTED`
  (seen after repeated failed submits / flaky mutations — the cart gets
  poisoned server-side). The rejection is *sticky per cart*. **Recovery: fresh
  cart** — `cart delete` then re-add the same items (add-items mints a new
  cart_uuid), preview + submit again. Do NOT conclude the store stopped
  accepting orders, and don't treat `asap_available:false` as proof.

## Replicating a previous order exactly ("my usual")

- **`order history` items[] DROPS OPTIONS** — it returns item names + qty only.
  Never build a replica from history alone.
- Correct source: **`order receipt --order-uuid <u>`** →
  `orders[].order_items[].options[].item_extra_option.{id,name}` = the exact
  option ids (same ids as the menu, `o_` prefix optional). Also has
  `special_instructions` if a note was saved.
- Flow: `order history --max 50 --days 365 --intent "checking past orders"`
  → find the store by name → `order receipt --order-uuid …` → map each line's
  options to `nested_options` (per-line! different options per line = separate
  `items[]` entries) → `cart add-items` all lines in one call → `cart list`
  to verify names/quantities → `order preview`.
- `order reorder --order-uuid <u>` is the shortcut (returns a NEW cart_uuid
  pre-loaded); still inspect with `cart list` before submitting.
- `--max` caps at **100**; `--days` widens the window. Grep `store_name`.

## Grocery / retail

```
dd-cli find-nearby-stores --vertical grocery --limit 5
dd-cli find-items --store-id <id> --query "coffee"      # per-query item_id
dd-cli item-details --store-id <id> --item-id <iid>     # + top-level menu_id
dd-cli cart add-items --store-id <id> --menu-id <mid> --intent "…" --items-json '...'
```

(`build-grocery-list` = stateless ingredient resolver; each call REPLACES the
list. Use `find-items`/`item-details` for most grocery adds.)

## Other commands

- `order history --max 10 [--days 90] [--include-group-order]` — past orders.
- `order status --order-uuid <u>` / `order receipt --order-uuid <u>`
- `order reorder --order-uuid <u>` → returns a NEW `cart_uuid`, then continue.
- `order checkout-url --cart-uuid <cart>` — browser fallback instead of
  charging.
- `promo list --store-id <id>` / `promo apply|remove --cart-uuid <cart>
  --promo-code C`
- `address list` / `address set --address-id <x> -y` / `address find -q "…"
  --limit 5` / `address add --place-id <x>` (add is ACCOUNT-WIDE default, no
  dedupe — confirm with user; check `address list` first).
- `payment-method` → `payment-method list`.
- `store-details --store-id <id>`.

## Piping dd-cli JSON to an interpreter

- Prefer writing to a temp file, then parsing the file, over piping `dd-cli`
  output straight into `python3 -c` / `jq` — some agent terminals flag
  pipe-to-interpreter calls (auto-approval noise).
- When parsing `cart list` / `add-items` echoes, **guard option-name lookups**:
  `cart list`'s `nested_options[]` carry `name` directly, but `add-items`'s
  echo nests it under `item_extra_option` (and items with no options have
  `nested_options: []`) — index defensively (`o.get('item_extra_option') or o`)
  instead of a bare key.

## Shell escaping gotchas (learned the hard way)

- Option names can contain **apostrophes** (e.g. `"Burn em'"`). Inlining
  `--items-json '[...]'` in bash **breaks** on the `'`. Safe paths:
  - build the JSON in Python and pass through `shlex.quote()`, or
  - write to a heredoc file and `$(cat file)` — never raw single-quotes with
    apostrophes.
- dd-cli has **no `--items-file` flag** — JSON must be an argument.
- Long multi-line shell one-liners with embedded pipes are fragile through an
  agent terminal; prefer small discrete calls.

## Preview / receipt anatomy (where to look)

- Totals: `quote.line_items[]` — `SUBTOTAL`, `DELIVERY_FEE`, `SERVICE_FEE`,
  `TAX` (`final_money.display_string`; DashPass shows discounted values).
  `quote.total_before_tip` = what the card charges **before tip**.
- Lines + resolved option names: `quote.store_order_cart.orders[].order_items[]
  → options[].item_extra_option.name`.
- Availability: `quote.delivery_availability.asap_available` /
  `asap_minutes_range` — **can be stale/misleading** (observed
  `asap_available:false` with a live ETA range while the store was open and
  accepting orders). Verify against the store page/UI (or just try) before
  declaring the store unavailable.

## Behaviors

- **After `order submit` succeeds, include the tracking link in your
  confirmation to the user**: `https://www.doordash.com/orders/<order_uuid>`
  (the uuid from the submit response) — **plus the delivery window** from
  `order status` (`quoted_delivery_time`, or the start/end pair when they
  differ), so the user knows when to expect it.
- **Confirm before `order submit`, always** (items, total, tip). Tip is
  **cents** (`--tip-cents`). Use a sensible default when the user doesn't
  specify (this skill's owner defaults to **$3 = 300**); make it a user
  preference if theirs differs.
- **Show prices** and the exact option set before ordering; for replicas, read
  the receipt FIRST, not history.
- `--fulfillment delivery|pickup`, `--priority` (express),
  `--scheduled-time` (ISO8601 UTC) for scheduled — pass consistently to
  **both** preview and submit.
- If a command fails, follow its message; don't improvise auth, don't silently
  retry batches, don't touch the browser. **Override**: the API can tell you
  "do not resubmit this cart" (`CART_SUBMIT_REJECTED`) — the real fix is a
  *fresh* cart (delete + re-add the same items), not dropping the store. That
  message describes cart state, not store availability.

## Token expired?

See `scripts/token-fetch.sh` — refreshes `DD_CLI_ACCESS_TOKEN`, preferring the
native keychain path (`dd-cli export-token` on a machine with a browser) and
falling back to a headless CDP-Chrome flow when no browser is available.
