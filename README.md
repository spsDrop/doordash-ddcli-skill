# doordash-ddcli-skill

A [Hermes Agent](https://hermes-agent.nousresearch.com) **skill** for ordering
food & grocery through DoorDash — built on the **official `dd-cli` binary**
rather than brittle browser automation.

It gives an agent *precise, reliable* control over the two things that are
fiddly by hand: **re-ordering a subset of a past order** and **walking a
store's modifier/option tree** — plus the retry, cart-cleanup and
line-by-line verification so a half-built cart never wedges a store.

> Works with any agent that can run a shell and read `SKILL.md` (Hermes,
> Claude Code, Cursor, …). The skill is a folder: `SKILL.md` is the
> instruction set, `scripts/` are the executable helpers.

## Why this beats browser automation

| | Browser automation | **dd-cli (this skill)** |
|---|---|---|
| Options/modifiers | Fumble the UI, names-only | `nested_options[]` with stable `option_id`s |
| Price | Scraped, flaky | `order preview` returns the exact quote |
| Reorder a subset | Re-do the whole cart by hand | `reorder <uuid> 2 5` keeps lines 2 & 5 only |
| Tip / promo / fulfillment | UI toggles | `--tip-cents`, `promo apply`, `--fulfillment` |
| Failure mode | Stuck on a modal | Deterministic JSON + bounded retries |

## What's in the box

```
SKILL.md               The skill (agent instruction set)
scripts/ddtools.py     Helper: reorder / read-options / write-options / carts
scripts/install-dd-cli.sh   Download + verify + install the official dd-cli
scripts/token-fetch.sh      Refresh DD_CLI_ACCESS_TOKEN (keychain or CDP)
requirements.txt       (stdlib-only — nothing to pip install)
```

## Quickstart

```bash
# 1. Install the official dd-cli binary (macOS Apple Silicon or glibc Linux)
bash scripts/install-dd-cli.sh

# 2. Sign in (opens a browser, saves to your OS keychain)
dd-cli login

# 3. Headless host? Copy a token over instead:
#    on a machine WITH a browser:   dd-cli export-token
#    on the headless box:           export DD_CLI_ACCESS_TOKEN="<that token>"
#    (or use:  bash scripts/token-fetch.sh)

# 4. Sanity check
dd-cli payment-method list --intent "verifying auth"
```

## The high-level call functions

Everything is driven by `dd-cli` on `$PATH`. The `ddtools.py` helper wraps the
two fiddly jobs so an agent doesn't hand-roll the bookkeeping:

| Call | What it does |
|---|---|
| `ddtools.py reorder <order_uuid> [idx…] [--cleanup]` | Re-order selected receipt lines (1-based) into a fresh, **verified** cart. Nukes stale carts, re-adds with the receipt's exact options (bounded retry), verifies line-by-line, previews. Leaves the cart open for `order submit` unless `--cleanup`. |
| `ddtools.py read-options --store-id S (--item-id I \| --query NAME)` | Flatten an item's modifier tree into flat rows an LLM can pick from (`kind`, `id`, `name`, `price`, `min/max`, `required`). |
| `ddtools.py write-options --store-id S --item-id I --picks '[…]'` | Turn a flat pick list back into a valid `nested_options` `items_json` entry, ready to paste into `cart add-items`. |
| `ddtools.py carts [--delete-all]` | List (or empty) every open cart across stores. |

The raw `dd-cli` verbs `ddtools` wraps (for the full flow):

```
search → menu → restaurant-item-details → cart list → cart add-items
→ cart list → order preview → order submit
```

See `SKILL.md` for the full command reference, cart-state behavior, and the
hard-won pitfalls (stale `asap_available`, sticky `CART_SUBMIT_REJECTED`,
apostrophes in option names, `--intent` required on every call, etc.).

## How it works (under the hood)

- **One shared command layer.** `ddtools.py` funnels every `dd-cli` call
  through a single `run_dd()` that unwraps the `{"content":[{"text": …}]}`
  envelope, strips `Error:` prefixes, and returns a dict (failures surface as
  `{"__dd_error__": …}` so callers branch on a key instead of exceptions).
- **Bounded, targeted retries.** On `cart add-items`, only recoverable errors
  retry: transient `ITEM_MUTATION_UNAVAILABLE` sleeps 15 s and re-adds *just
  the failing items*; rejected options retry bare (menu defaults); terminal
  `ITEM_UNAVAILABLE` / required-option failures stop and report. No whole-batch
  replays (quantities would merge and duplicate lines).
- **Verification is the source of truth.** A reorder only PASSes when
  `cart show` matches the selected lines exactly on (item, option-set,
  quantity).
- **No leaked carts.** `--cleanup` reaps the cart on *every* run — a FAIL
  leaves nothing open.

## Security notes

- `--intent` is passed on **every** call and may be harvested by DoorDash.
  Keep it generic and banal — never put names, addresses, or payment info in it.
- **`order submit` charges the card.** Confirm items, total, and tip with the
  user first.
- The `DD_CLI_ACCESS_TOKEN` is as sensitive as a password. `token-fetch.sh`
  writes it with `chmod 600` and never logs it to stdout. Treat it like a
  secret; `.gitignore` excludes token/env files.

## Requirements

- **Python 3.9+** (standard library only — no pip installs).
- **macOS (Apple Silicon)** or **glibc Linux x86_64** (Debian 11+, Ubuntu
  20.04+, RHEL 9, Amazon Linux 2023, and similar). dd-cli ships builds for
  these two platforms only.
- **`dd-cli`** — install with `scripts/install-dd-cli.sh` (downloads the
  official release from `doordash-oss/doordash-cli`, verifies SHA256).
- A **browser** for `dd-cli login` / `export-token`, *or* a headless Chrome
  on a CDP port for the fallback token path.

## License

[MIT](LICENSE) — © 2026 Jesse Foltz. The `dd-cli` binary is separate
third-party software from `doordash-oss/doordash-cli`, distributed as-is; this
repo only contains the skill, the Python helper, and the install scripts.
