#!/usr/bin/env python3
"""ddtools — DoorDash dd-cli helper: subset reorders + option-tree flatten/resolve.

A thin, deterministic wrapper around the official `dd-cli` binary. It exists
for the two things that are fiddly to do by hand — re-ordering a *subset* of a
past receipt into a fresh, verified cart, and walking a store's option tree —
while doing the retry / cleanup / verify bookkeeping so the caller (usually an
agent) can't wedge a store with a half-built cart.

Subcommands (see each function's docstring for the full argument reference):

  reorder      Re-order selected receipt lines into a fresh, verified cart.
  read-options Flatten an item's option tree into flat, pickable rows.
  write-options Resolve a flat pick list back into a valid `nested_options`.
  carts        List open carts; --delete-all empties every open cart.

Conventions baked into the tool:
  * Every dd-cli call is passed a generic, banal `--intent` (no personal data).
  * `dd-cli --json-output` wraps payloads in {"content":[{"text": "..."}]} —
    that envelope is unwrapped automatically; plain JSON is handled too.
  * Auth is via the DD_CLI_ACCESS_TOKEN env var (or a bashrc export, see
    token_env()). Nothing here stores tokens; the keychain owns them.

Exit codes: 0 on success (a reorder PASS, a clean read/write, an empty cart
list), 1 on a failed/FAILED result, 2 on usage error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The dd-cli binary, resolved from $PATH at call time.
DD = "dd-cli"

#: One banal intent string shared by every dd-cli call. Keep it generic —
#: never put addresses, names, or other personal data in it.
INTENT = (
    "Summary: Help the user reorder or customize a past DoorDash order.\n"
    "user prompt/purpose: \"reorder items from a previous order\""
)

#: How many times cart add-items will be retried on recoverable errors.
MAX_ADD_ATTEMPTS = 3

#: Seconds to wait before a transient-retry of add-items.
TRANSIENT_BACKOFF_S = 15


def die(msg, code=1):
    """Print `ERROR: msg` to stderr and exit with `code`.

    Args:
        msg: Human-readable failure reason.
        code: Process exit code (default 1; 2 is reserved for usage errors).
    """
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------------------
# Common command layer
# ---------------------------------------------------------------------------

def token_env():
    """Return an env mapping for dd-cli subprocesses, with a live token.

    The token comes from DD_CLI_ACCESS_TOKEN in the environment. If that is
    unset, fall back to a bashrc-sourced token (headless hosts commonly keep
    the token in a top-level `export` in ~/.bashrc) so the call still works.
    The bashrc token is *not* written back anywhere — it only fills the
    subprocess env.

    Returns:
        dict: A copy of os.environ, with DD_CLI_ACCESS_TOKEN set if found.
    """
    env = dict(os.environ)
    if not env.get("DD_CLI_ACCESS_TOKEN"):
        try:
            src = open(os.path.expanduser("~/.bashrc")).read()
            m = re.search(r'DD_CLI_ACCESS_TOKEN="?([A-Za-z0-9._~+/=-]+)"?', src)
            if m:
                env["DD_CLI_ACCESS_TOKEN"] = m.group(1)
        except OSError:
            pass  # no bashrc / not readable — run with whatever env we have
    return env


def run_dd(*args, intent=INTENT, timeout=90):
    """Run one dd-cli command and return its unwrapped JSON payload.

    Args:
        *args: dd-cli arguments, e.g. ("cart", "list", "--store-id", "160").
        intent: The `--intent` text to attach (must stay generic/banal).
        timeout: Per-call wall-clock limit in seconds.

    Returns:
        dict: The parsed JSON payload. On any failure the returned dict is
        `{"__dd_error__": <message>, "rc": <returncode>}` — callers check for
        the `"__dd_error__"` key rather than raising.
    """
    cmd = [DD, "--json-output", *[str(a) for a in args], "--intent", intent]
    p = subprocess.run(cmd, capture_output=True, text=True,
                       timeout=timeout, env=token_env())

    # On failure dd-cli puts "Error: …" on stderr (rc != 0) and leaves stdout
    # empty; on a successful HTTP call that reports item-level errors
    # (success:false + item_errors) the JSON comes out on stdout.
    raw = p.stdout.strip()
    if raw.startswith("Error: "):
        raw = raw.split("Error: ", 1)[1]
    if not raw:
        raw = p.stderr.strip()
        if raw.startswith("Error: "):
            raw = raw.split("Error: ", 1)[1]
    try:
        d = json.loads(raw)
    except ValueError:
        return {"__dd_error__": (raw or "no output")[:500], "rc": p.returncode}

    # Unwrap the {"content":[{"text": "<json>"}]} envelope (dd-cli v0.2.x).
    if isinstance(d, dict) and isinstance(d.get("content"), list) and d["content"]:
        inner = d["content"][0].get("text", "")
        try:
            d = json.loads(inner)
        except ValueError:
            d = {"__dd_error__": inner[:500]}
    return d


# ---------------------------------------------------------------------------
# Macro entry points (subcommand handlers)
# ---------------------------------------------------------------------------

def cmd_reorder(argv):
    """reorder <order_uuid> [indices...] [--cleanup] [--json]

    Re-order the selected receipt lines (1-based indices into the order's
    order_items; default = all lines) into a fresh cart, then verify and
    preview.

    Args:
        order_uuid: The past order's uuid (must be within the last 100 / 365d).
        indices: Optional 1-based line numbers to keep; omitted = keep all.
        --cleanup: Dry-run mode — delete the verified cart on EVERY run
                   (success or failure) so nothing is left open.
        --json: Machine output only (single JSON object on stdout).

    Behavior:
        1. Delete any pre-existing open cart at the store.
        2. add-items the selected lines with their receipt options, with the
           bounded-retry logic in add_with_retry().
        3. Verify the cart line-by-line against the selected lines.
        4. Run order preview (real prices, no charge).
        Leaves the cart OPEN for `order submit` unless --cleanup.
    """
    a = parse_reorder_args(argv)
    order = order_by_uuid(a.order_uuid)
    if order is None:
        die(f"order {a.order_uuid} not found in last 100 orders (365 days)")
    if order.get("is_reorderable") is False:
        die(f"order {a.order_uuid} not reorderable "
            f"({order.get('fulfillment_type')}/{order.get('order_target')})")

    store_id = str(order["store_id"])
    lines = receipt_lines(a.order_uuid)
    sel = pick_lines(lines, a.indices)
    log = []

    if not a.json:
        print(f"== {order.get('store_name')} ({a.order_uuid[:8]}…)")
        for i, line in enumerate(lines, 1):
            mark = "drop" if a.indices and i not in a.indices else "KEEP"
            print(f"  [{i}] ({mark}) {line_label(line)}")
        print(f"  reordering {len(sel)} of {len(lines)} lines")

    # A transient "cart list" hiccup is NOT logged as a warning — an empty
    # open-cart set is the normal state, and a flaky list doesn't block the
    # add (add-items mints/returns its own cart_uuid).
    for c in open_carts_for(store_id):
        log.append(f"deleted pre-existing open cart {c['cart_uuid']} "
                   f"({delete_cart(c['cart_uuid'])})")

    try:
        menu_id = str(get_menu(store_id).get("menu_id"))
    except MenuNotFoundError as e:
        log.append(f"menu unavailable: {e}")
        print_reorder_result(_reorder_result(order, a.order_uuid, store_id, sel,
                                             lines, None, {}, log, env=True),
                             a.json)
        return 1

    r, notes = add_with_retry(store_id, menu_id, build_items_json(sel), log)
    log.extend(notes)
    if "__dd_error__" in r:
        # dd-cli hard error (transient CART_MUTATION_UNAVAILABLE, ITEM_
        # UNAVAILABLE, closed-store message…). No JSON back, so we can't see a
        # partial cart — best effort: drop whatever open cart exists.
        for c in open_carts_for(store_id):
            log.append(f"hard add error; deleted open cart {c['cart_uuid']} "
                       f"({delete_cart(c['cart_uuid'])})")
        log.append(f"hard add error: {r['__dd_error__'][:400]}")
        print_reorder_result(_reorder_result(order, a.order_uuid, store_id, sel,
                                             lines, None, {}, log),
                             a.json)
        return 1

    cart_uuid = r.get("cart_uuid") or (r.get("cart") or {}).get("id")
    if not cart_uuid:
        die(f"cart add-items: no cart_uuid in response: {json.dumps(r)[:400]}")

    # Source of truth = the cart itself. A success:false with item_errors
    # (partial add) just means verify_cart() will log the missing line(s) and
    # ok goes False — the cleanup below still reaps the leftover cart.
    cart = verify_cart(cart_uuid, sel, log)
    preview = run_preview(cart_uuid, log)

    # PASS = cart matches the re-ordered lines exactly. `asap_available:false`
    # just means the store is closed / scheduled-ahead right now — the cart is
    # still correct and can be scheduled or submitted when open; it does NOT
    # make the reorder wrong.
    ok = cart is not None and not log
    if a.cleanup:
        # Reap the cart on EVERY run with --cleanup — a FAIL must not leak an
        # open cart either (dry-run mode leaves nothing behind).
        deleted = delete_cart(cart_uuid)
        if not ok:
            log.append(f"--cleanup: deleted leftover cart {cart_uuid} ({deleted})")
        if ok:
            ok = deleted

    result = _reorder_result(order, a.order_uuid, store_id, sel, lines,
                             None if (ok and a.cleanup) else cart_uuid,
                             preview, log)
    result["ok"] = ok
    print_reorder_result(result, a.json)
    return 0 if ok else 1


def cmd_read_options(argv):
    """read-options --store-id S (--item-id I | --query NAME) [--menu-id M]

    Flatten an item's option tree (extras -> options -> extras …) into a flat
    row list an LLM can pick from without walking the nested structure.

    Args:
        --store-id: Store id (required).
        --item-id: Item id, bare or `i_`-prefixed.
        --query: Match an item by (substring of its) menu name, instead of id.
        --menu-id: Menu id; defaults to the store's current menu.

    Output rows carry {kind, name, id, top, depth} plus kind-specific fields:
        kind=group  : a selection group (min/max/required).
        kind=option : a pickable leaf option.
        kind=choice : an option whose children live in nested extras[] (the
                      "choice itself" — its children are separate option rows
                      with the same top and depth+1).
    Pass `parent` when an option id could collide across branches.
    """
    a = parse_read_options_args(argv)
    if not a.item_id and not a.query:
        die("need --item-id or --query")
    item_id, item, menu = load_item(a.store_id, a.item_id, a.menu_id, a.query)
    rows, by_id, _ = [], {}, {}
    walk_tree(item.get("extras") or [], rows, by_id, {})
    out = {
        "store_id": a.store_id, "item_id": item_id,
        "menu_id": str(menu.get("menu_id")), "item_name": item.get("name"),
        "orderable": item.get("is_orderable"),
        "has_required_modifiers": item.get("has_required_modifiers"),
        "groups": sum(1 for r in rows if r["kind"] == "group"),
        "leaves": sum(1 for r in rows if r["kind"] != "group"),
        "rows": rows,
        "hint": ("pick rows where kind=option (or the child rows under a "
                 "kind=choice enum) and feed {id, parent?, name, quantity} to "
                 "`ddtools write-options`. `parent` = the top-level option id a "
                 "nested pick belongs under (use when option ids could "
                 "collide). required=true groups need >= min selections unless "
                 "is_default options cover them and you pass --keep-defaults."),
    }
    print(json.dumps(out, indent=1))
    return 0


def cmd_write_options(argv):
    """write-options --store-id S --item-id I --picks JSON [--quantity N]
                   [--keep-defaults] [--menu-id M]

    Resolve a flat selection (from read-options) back into a valid
    `nested_options` items-json entry for `cart add-items`.

    Args:
        --store-id: Store id (required).
        --item-id: Item id, bare or `i_`-prefixed (required).
        --picks: JSON array of {"id","parent"?,"name"?,"quantity"?} (required).
        --quantity: Item quantity to add (default 1).
        --keep-defaults: Omit default_handling so untouched groups keep menu
                         defaults; otherwise default_handling="exact" is set.
        --menu-id: Menu id; defaults to the store's current menu.

    Emits {"items_json": "<paste-ready array>", "entry": {...}, "warnings":
    [...], "resolved": [option names matched]}.
    """
    a = parse_write_options_args(argv)
    picks = json.loads(a.picks)
    if not isinstance(picks, list):
        die("--picks must be a JSON array")
    item_id, item, menu = load_item(a.store_id, a.item_id, a.menu_id)
    rows, by_id, _ = [], {}, {}
    walk_tree(item.get("extras") or [], rows, by_id, {})
    nested, warnings = resolve_picks(item, picks, by_id)

    entry = {"item_id": item_id, "item_name": item.get("name"),
             "quantity": a.quantity, "nested_options": nested}
    if not a.keep_defaults:
        entry["default_handling"] = "exact"
    out = {
        "items_json": json.dumps([entry]),
        "entry": entry,
        "warnings": warnings,
        "resolved": [by_id[str(pk.get("id", "")).removeprefix("o_")]["name"]
                     for pk in picks
                     if str(pk.get("id", "")).removeprefix("o_") in by_id],
    }
    print(json.dumps(out, indent=1))
    return 0


def cmd_carts(argv):
    """carts [--delete-all]

    List every open cart across all stores; --delete-all empties each one.

    Args:
        --delete-all: Delete every open cart (prints a per-cart result).

    Output: "no open carts", or one line per cart: "open: <uuid>  <store>  N
    items" (or "deleted …"/"FAILED to delete …" under --delete-all).
    """
    a = parse_carts_args(argv)
    cl = run_dd("cart", "list")
    if "__dd_error__" in cl:
        die(f"cart list failed: {cl['__dd_error__']}")
    carts = cl.get("carts", [])
    if not carts:
        print("no open carts")
        return 0
    rc = 0
    for c in carts:
        cu = c.get("cart_uuid")
        name = c.get("store_name") or (c.get("store") or {}).get("name") or "?"
        n = c.get("items_count", len(c.get("items", [])))
        if a.delete_all:
            ok = bool(delete_cart(cu))
            rc |= 0 if ok else 1
            print(f"{'deleted' if ok else 'FAILED to delete'} {cu} "
                  f"({name}, {n} items)")
        else:
            print(f"open: {cu}  {name}  {n} items")
    return rc


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_reorder_args(argv):
    """Parse `reorder` arguments. See cmd_reorder() for the flag reference."""
    p = argparse.ArgumentParser(prog="ddtools reorder")
    p.add_argument("order_uuid")
    p.add_argument("indices", nargs="*", type=int,
                   help="1-based receipt line indices to keep (default: all)")
    p.add_argument("--cleanup", action="store_true",
                   help="delete the verified cart after success (dry-run mode)")
    p.add_argument("--json", action="store_true", help="machine output only")
    return p.parse_args(argv)


def parse_read_options_args(argv):
    """Parse `read-options` arguments. See cmd_read_options()."""
    p = argparse.ArgumentParser(prog="ddtools read-options")
    p.add_argument("--store-id", required=True)
    p.add_argument("--item-id", default=None)
    p.add_argument("--query", default=None, help="match item by menu name")
    p.add_argument("--menu-id", default=None)
    return p.parse_args(argv)


def parse_write_options_args(argv):
    """Parse `write-options` arguments. See cmd_write_options()."""
    p = argparse.ArgumentParser(prog="ddtools write-options")
    p.add_argument("--store-id", required=True)
    p.add_argument("--item-id", required=True, help="bare or i_ prefixed")
    p.add_argument("--picks", required=True,
                   help='JSON array: [{"id","parent"?,"name"?,"quantity"?}]')
    p.add_argument("--quantity", type=int, default=1,
                   help="item quantity (default 1)")
    p.add_argument("--keep-defaults", action="store_true",
                   help="omit default_handling so untouched groups keep "
                        "menu defaults")
    p.add_argument("--menu-id", default=None)
    return p.parse_args(argv)


def parse_carts_args(argv):
    """Parse `carts` arguments. See cmd_carts()."""
    p = argparse.ArgumentParser(prog="ddtools carts")
    p.add_argument("--delete-all", action="store_true")
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# Shared dd-cli building blocks (used by multiple subcommands)
# ---------------------------------------------------------------------------

class MenuNotFoundError(Exception):
    """Store has no menu right now (closed/renumbered/stale id) — environmental."""


def get_menu(store_id):
    """Fetch a store's current menu.

    Args:
        store_id: Store id (str or int).

    Returns:
        dict: The menu payload (carries menu_id + items[]).

    Raises:
        MenuNotFoundError: The dd-cli call failed, or the store has no menu
            right now (closed / renumbered / stale id) — an environmental,
            not-a-tool-bug condition.
    """
    m = run_dd("menu", "--store-id", store_id)
    if "__dd_error__" in m:
        raise MenuNotFoundError(f"menu call failed: {m['__dd_error__'][:200]}")
    if m.get("success") is False or not m.get("menu_id"):
        raise MenuNotFoundError(
            f"no menu for store {store_id} "
            f"({(m.get('message') or 'MENU_NOT_FOUND')[:160]}) — "
            "store is closed/renumbered or the order is too old; "
            "environmental, not a tool bug")
    return m


def open_carts_for(store_id=None):
    """Return the list of open carts (for a store, or all when store_id is None).

    A flaky `cart list` is treated as "no open carts" — an empty open-cart set
    is the normal state and a flaky list never blocks the add (add-items
    mints/returns its own cart_uuid).

    Args:
        store_id: Restrict to this store; None = all open carts.

    Returns:
        list[dict]: Open cart dicts (each has cart_uuid, store_name, …).
    """
    args = ["cart", "list"] + (["--store-id", store_id] if store_id else [])
    cl = run_dd(*args)
    if "__dd_error__" in cl:
        return []
    return cl.get("carts", []) or []


def delete_cart(cart_uuid):
    """Delete (empty) an open cart.

    Args:
        cart_uuid: The cart to empty; the next add mints a fresh cart.

    Returns:
        str: "ok=True" / "ok=False" / "error: <msg>" — safe to embed in a log.
    """
    d = run_dd("cart", "delete", "--cart-uuid", cart_uuid)
    if "__dd_error__" in d:
        return f"error: {d['__dd_error__'][:120]}"
    return f"ok={bool(d.get('success'))}"


def order_by_uuid(uuid):
    """Find a past order by uuid within the last 100 orders / 365 days.

    Args:
        uuid: The order uuid to look for.

    Returns:
        dict | None: The matching order dict, or None if not found (a
        hard dd-cli failure dies here rather than returning None).
    """
    h = run_dd("order", "history", "--max", "100", "--days", "365")
    if "__dd_error__" in h:
        die(f"order history failed: {h['__dd_error__']}")
    for o in h.get("orders", []):
        if o.get("order_uuid") == uuid:
            return o
    return None


def receipt_lines(uuid):
    """Return the receipt's order_items (the exact lines + options bought).

    Args:
        uuid: The order uuid.

    Returns:
        list[dict]: order_items[] from the receipt — the authoritative source
        for re-ordering (history drops options; the receipt does not).
    """
    r = run_dd("order", "receipt", "--order-uuid", uuid)
    if "__dd_error__" in r:
        die(f"order receipt failed: {r['__dd_error__']}")
    orders = r.get("orders", [r])
    return orders[0].get("order_items", [])


def run_preview(cart_uuid, log):
    """Run order preview (real prices, no charge) and log any failure.

    Args:
        cart_uuid: The open cart to preview.
        log: List to append a human-readable note to on failure.

    Returns:
        dict: {"total_before_tip": str, "asap_available": bool} (empty if
        preview failed).
    """
    pv = run_dd("order", "preview", "--cart-uuid", cart_uuid)
    if "__dd_error__" in pv:
        log.append(f"preview failed: {pv['__dd_error__']}")
        return {}
    q = pv.get("quote", {})
    return {
        "total_before_tip": (q.get("total_before_tip") or {}).get("display_string"),
        "asap_available": (q.get("delivery_availability") or {}).get("asap_available"),
    }


# ---------------------------------------------------------------------------
# Receipt line helpers
# ---------------------------------------------------------------------------

def pick_lines(lines, indices):
    """Return [(idx, line)] for 1-based indices; empty indices = all lines.

    Args:
        lines: The receipt's order_items[].
        indices: 1-based line numbers to keep; empty = keep every line.

    Returns:
        list[tuple[int, dict]]: (1-based index, line) for each kept line.
    """
    if not indices:
        return list(enumerate(lines, 1))
    out = []
    for i in indices:
        if not 1 <= i <= len(lines):
            die(f"index {i} out of range (order has {len(lines)} lines)")
        out.append((i, lines[i - 1]))
    return out


def line_label(line):
    """A human-readable label for a receipt line: '2x Name [opt, opt]'.

    Args:
        line: A receipt order_item dict.

    Returns:
        str: The formatted label.
    """
    opts = [op.get("item_extra_option", {}).get("name")
            for op in line.get("options", [])]
    opts = [o for o in opts if o]
    s = f"{line.get('quantity', 1)}x {line['item']['name']}"
    return f"{s} [{', '.join(opts)}]" if opts else s


def line_opt_ids(line):
    """A hashable key of a line's option ids (sorted, str, empty-ids dropped).

    Args:
        line: A receipt order_item dict.

    Returns:
        tuple[str, ...]: The option ids, sorted.
    """
    return tuple(sorted(str(op.get("id", "")) for op in line.get("options", [])
                        if op.get("id")))


def build_items_json(sel):
    """One cart add-items items[] entry per kept receipt line, with options.

    Args:
        sel: [(idx, line)] as returned by pick_lines().

    Returns:
        list[dict]: items[] — each {item_id, item_name, quantity} plus a
        nested_options[] when the line had options.
    """
    items = []
    for _, line in sel:
        it = {
            "item_id": str(line["item"]["id"]),
            "item_name": line["item"]["name"],
            "quantity": int(line.get("quantity", 1)),
        }
        opts = []
        for op in line.get("options", []):
            ieo = op.get("item_extra_option", {})
            if ieo.get("id"):
                opts.append({
                    "id": str(ieo["id"]),
                    "name": ieo.get("name", ""),
                    "quantity": int(op.get("quantity", ieo.get("quantity", 1)) or 1),
                })
        if opts:
            it["nested_options"] = opts
        items.append(it)
    return items


# ---------------------------------------------------------------------------
# Cart mutation (bounded retry) + verification
# ---------------------------------------------------------------------------

def _err_ref(e):
    """Return (item_ref, message) for an item_error, across dd-cli shapes.

    dd-cli nests the offending item under e["request"]; older shapes put
    item_id/item_name at the top level. Returns the first non-empty of each.

    Args:
        e: One item_errors[] entry from a cart add-items response.

    Returns:
        tuple[str, str]: (best-effort item id/name, error message).
    """
    rq = e.get("request") or {}
    return (str(rq.get("item_id") or e.get("item_id") or
                rq.get("item_name") or e.get("item_name") or ""),
            str(e.get("error_message") or (rq.get("error_message") or "")))


def add_with_retry(store_id, menu_id, items, log):
    """cart add-items with bounded, targeted retries.

    Adds `items` to the store's cart, retrying only what's recoverable:
      * transient ITEM_MUTATION_UNAVAILABLE (no required_options listed):
        sleep 15s and re-add ONLY the failing items;
      * options rejected (no required_options): retry those lines bare (menu
        defaults apply);
      * ITEM_UNAVAILABLE (store-side "not orderable right now") and
        required-options failures are terminal — logged and returned as-is.

    Args:
        store_id: Store id.
        menu_id: Menu id (usually == store id).
        items: items[] as from build_items_json().
        log: List to append human-readable notes to.

    Returns:
        tuple[dict, list]: (final dd-cli response, notes). The response is the
        last one received; check for "__dd_error__".
    """
    pending = list(items)
    notes = []
    last = None
    for _ in range(MAX_ADD_ATTEMPTS):
        if not pending:
            break
        r = run_dd("cart", "add-items", "--store-id", store_id,
                   "--menu-id", menu_id, "--items-json", json.dumps(pending))
        if "__dd_error__" in r:
            return r, notes
        last = r
        errs = r.get("item_errors") or []

        transient = [e for e in errs
                     if "ITEM_MUTATION_UNAVAILABLE" in _err_ref(e)[1]
                     and not e.get("required_options")]
        required = [e for e in errs if e.get("required_options")]
        # store-side "not orderable right now" — terminal; retrying bare
        # cannot fix it (item is out of stock / store closed).
        unavailable = [e for e in errs if "ITEM_UNAVAILABLE" in _err_ref(e)[1]]
        other = [e for e in errs
                 if e not in transient and e not in required
                 and e not in unavailable]
        if not errs:
            return r, notes

        if required:
            notes.append("required-options failures (options can't be inferred): "
                         + json.dumps(
                             [{"item": e.get("item_name") or e.get("item_id"),
                               "required_options": e["required_options"]}
                              for e in required])[:1500])
            return r, notes
        if unavailable:
            notes.append("ITEM_UNAVAILABLE (store-side, not a tool bug — item(s) "
                         f"{', '.join(_err_ref(e)[0] for e in unavailable)} "
                         "not orderable right now): "
                         + str(r.get("message", ""))[:200])
            return r, notes
        if other:
            bad_ids = {_err_ref(e)[0] for e in other} - {""}
            notes.append("add rejected options for: " + ", ".join(
                f"{bid} ({_err_ref(e)[1][:100]})" for bid, e in zip(bad_ids, other)))
            pending = [x for x in items if str(x["item_id"]) in bad_ids]
            for x in pending:
                x.pop("nested_options", None)
                x.pop("default_handling", None)
                notes.append(f"retrying {x['item_id']} bare (menu defaults apply)")
            if not pending:
                return r, notes
            continue
        # transient: back off, then re-add only the failing items.
        fail_ids = {str(e.get("item_id")) for e in transient}
        notes.append(f"transient errors on {len(transient)} item(s); "
                     f"retrying in {TRANSIENT_BACKOFF_S}s")
        time.sleep(TRANSIENT_BACKOFF_S)
        pending = [x for x in items if str(x["item_id"]) in fail_ids]
    if last is None:
        last = run_dd("cart", "list", "--store-id", store_id)
    return last, notes


def verify_cart(cart_uuid, sel, log):
    """Verify a cart matches the selected receipt lines exactly.

    cart show is the source of truth. Compares (item, option-set) quantity
    sums between the cart and the expected lines, logging each discrepancy.

    Args:
        cart_uuid: The cart to verify.
        sel: [(idx, line)] as from pick_lines().
        log: List to append discrepancies / failures to.

    Returns:
        dict | None: The cart payload, or None if `cart show` failed.
    """
    cs = run_dd("cart", "show", "--cart-uuid", cart_uuid)
    if "__dd_error__" in cs:
        log.append(f"cart show failed: {cs['__dd_error__']}")
        return None
    cart = cs.get("cart", cs)
    actual = {}
    for it in cart.get("items", []):
        key = (str(it.get("item_id")),
               tuple(sorted(str(o.get("id")) for o in it.get("nested_options", [])
                            if o.get("id"))))
        actual[key] = actual.get(key, 0) + int(it.get("quantity") or 0)
    expected = {}
    for _, line in sel:
        key = (str(line["item"]["id"]), line_opt_ids(line))
        expected[key] = expected.get(key, 0) + int(line.get("quantity", 1))
    for key, qty in expected.items():
        a = actual.get(key)
        if a is None:
            log.append(f"line missing from cart: item {key[0]} opts {list(key[1])}")
        elif a != qty:
            log.append(f"qty mismatch item {key[0]}: expected {qty}, cart has {a}")
    for key, a in actual.items():
        if key not in expected:
            log.append(f"unexpected cart line item {key[0]} qty {a}")
    return cart


def _reorder_result(order, order_uuid, store_id, sel, lines, cart_uuid,
                    preview, log, env=False):
    """Build the reorder result dict (shared by every exit path).

    `ok` is always False here — the caller sets it after cleanup is applied.
    """
    return {
        "ok": False,
        "order_uuid": order_uuid,
        "store": order.get("store_name"),
        "store_id": store_id,
        "lines_kept": [i for i, _ in sel],
        "lines_total": len(lines),
        "cart_uuid": cart_uuid,
        "preview": preview,
        "log": log,
        **({"env": True} if env else {}),
    }


def print_reorder_result(result, json_only):
    """Print the reorder result as JSON or as a human-readable block."""
    if json_only:
        print(json.dumps(result, indent=1))
        return
    if result["cart_uuid"]:
        print(f"  cart_uuid: {result['cart_uuid']} (open — submit with "
              f"`dd-cli order submit --cart-uuid {result['cart_uuid']}`)")
    if result["preview"]:
        print(f"  preview: total_before_tip={result['preview'].get('total_before_tip')} "
              f"asap={result['preview'].get('asap_available')}")
    for l in result["log"]:
        print("  note:", l)
    if result.get("env"):
        print("  FAIL (environmental — no menu right now)")
    else:
        print("  PASS" if result["ok"] else "  FAIL")


# ---------------------------------------------------------------------------
# Option tree: load / walk / resolve
# ---------------------------------------------------------------------------

def load_item(store_id, item_id, menu_id=None, query=None):
    """Load an item's detail plus its store's menu.

    Args:
        store_id: Store id.
        item_id: Item id, bare or `i_`-prefixed; ignored when query is set.
        menu_id: Menu id; defaults to the store's current menu.
        query: Optional — match an item by (substring of its) menu name.

    Returns:
        tuple[str, dict, dict]: (bare item_id, item detail dict, menu dict).
    """
    m = get_menu(store_id)
    menu_id = str(menu_id or m.get("menu_id"))
    if query:
        hits = [it for it in m.get("items", [])
                if query.lower() in str(it.get("name", "")).lower()]
        if not hits:
            die(f"--query {query!r} matched no menu item at store {store_id}")
        item_id = hits[0].get("item_id") or hits[0].get("id")
    item_id = str(item_id).removeprefix("i_")
    d = run_dd("restaurant-item-details", "--store-id", store_id,
               "--menu-id", menu_id, "--item-id", f"i_{item_id}")
    if "__dd_error__" in d:
        die(f"restaurant-item-details failed: {d['__dd_error__']}")
    return item_id, d.get("item", d), m


def walk_tree(extras, rows, by_id, top_id=None, depth=0):
    """Flatten extras->options->extras recursively into flat rows.

    Every row: {kind, name, id, top, depth} plus kind-specific fields.
      kind=group  : a selection group; min/max/required.
      kind=option : leaf option (pickable).
      kind=choice : option whose children live in nested extras[] (the
                    "choice itself" — its children are separate option rows
                    with the same top and depth+1).
      top   — id of the top-level option this row IS (depth 0) or belongs
              under (depth > 0). write-options anchors picks by `id`.
      depth — 0 for top-level options, 1+ for nested children.
    `path` on depth-0 rows is 'g.o' (indices into that level); nested rows
    carry `parent` (the choice id) instead — id+parent chains uniquely locate
    any leaf regardless of tree shape.

    Args:
        extras: The item's extras[] (top-level option groups).
        rows: Out-list — flat rows are appended here.
        by_id: Out-dict — option id -> its row, for resolve_picks().
        top_id: The top-level option id the current branch belongs under.
        depth: Recursion depth (0 = top level).
    """
    for gi, grp in enumerate(extras):
        gpath = f"{gi}" if depth == 0 else ""
        rows.append({
            "kind": "group",
            "name": grp.get("title"),
            "top": top_id if depth else None,
            "depth": depth,
            **({"path": gpath} if depth == 0 else {}),
            "min": grp.get("min_num_options"),
            "max": grp.get("max_num_options"),
            "required": (grp.get("min_num_options") or 0) > 0,
        })
        for oi, opt in enumerate(grp.get("options", [])):
            oid = str(opt.get("option_id", "")).removeprefix("o_")
            sub = opt.get("extras") or []
            row = {
                "kind": "choice" if sub else "option",
                "name": opt.get("name"),
                "id": oid,
                "top": top_id if top_id else oid,
                "depth": depth,
                "price": opt.get("price"),
                "is_default": opt.get("is_default"),
            }
            if depth == 0:
                row["path"] = f"{gi}.{oi}"
            elif top_id:
                row["parent"] = top_id
            if sub:
                row["enum"] = [s.get("title") for s in sub]
            rows.append(row)
            by_id.setdefault(oid, row)
            if sub:
                walk_tree(sub, rows, by_id, top_id=oid, depth=depth + 1)


def resolve_picks(item, picks, by_id):
    """Resolve flat picks into a valid nested_options structure.

    A pick references ANY option row from read-options by `id` (option ids are
    unique per item; if an id ever repeats across branches, pass `parent` =
    the top-level option id it belongs to to disambiguate).
      - depth-0 pick: its own top-level nested_options entry, quantity as given.
      - depth>0 pick (child of a choice): attaches under its `top` entry's
        options[]; the top entry is created implicitly with quantity 1 if it
        wasn't itself picked (children imply the parent choice is selected).

    Args:
        item: The item detail dict (needs extras[]).
        picks: list of {"id","parent"?,"name"?,"quantity"?}.
        by_id: option id -> row map, as built by walk_tree().

    Returns:
        tuple[list, list]: (nested_options, warnings). Warnings flag name
        mismatches and over-max selections; hard errors (unknown id, parent
        mismatch, under-min) die.
    """
    tree = item.get("extras") or []
    warnings = []
    tops = {}   # top id -> {"id","name","quantity","children":{id:row}}
    for pk in picks:
        pid = str(pk.get("id", "")).removeprefix("o_")
        if pid not in by_id:
            die(f"pick id {pid!r} not in tree; run read-options for valid ids")
        row = by_id[pid]
        if pk.get("name") and pk["name"].lower() not in (row.get("name") or "").lower() \
                and (row.get("name") or "").lower() not in pk["name"].lower():
            warnings.append(f"pick {pid}: name mismatch (tree: {row.get('name')!r}, "
                            f"you gave {pk['name']!r})")
        qty = int(pk.get("quantity", 1) or 1)
        parent_hint = str(pk.get("parent", "")).removeprefix("o_")
        top = row.get("top") or pid
        if parent_hint and parent_hint != top:
            die(f"pick {pid}: parent {parent_hint!r} disagrees with tree "
                f"(belongs under {top!r})")
        if row.get("depth", 0) > 0:
            # child pick: ensure the top entry exists
            t = tops.setdefault(top, {"id": top,
                                      "name": by_id.get(top, {}).get("name"),
                                      "quantity": 0, "children": {}})
            if t["quantity"] == 0 and t["children"] is not None:
                t["quantity"] = 1  # children imply the parent choice is selected
            c = t["children"].setdefault(pid, row)
            c["quantity"] = c.get("quantity", 0) + qty
        else:
            t = tops.setdefault(pid, {"id": pid, "name": row.get("name"),
                                      "quantity": 0, "children": {}})
            t["quantity"] += qty
    # min/max checks for top-level groups touched. Children count toward their
    # parent group: selecting a child implies its parent choice.
    # NOTE: defaults only help when default_handling != "exact".
    for gi, grp in enumerate(tree):
        ids = set()
        for opt in grp.get("options", []):
            oid = str(opt.get("option_id", "")).removeprefix("o_")
            if opt.get("extras") or oid in tops:
                ids.add(oid)
        if not ids:
            continue
        # top entries: a child pick under choice X always creates tops[X]
        # (qty 1 implied), so summing tops covers both direct and nested picks
        selected = sum(tops[oid]["quantity"] for oid in ids if oid in tops)
        has_default = any(o.get("is_default") for o in grp.get("options", []))
        mn = grp.get("min_num_options") or 0
        mx = grp.get("max_num_options")
        if selected < mn and not has_default:
            die(f"group {gi} {grp.get('title')!r}: selected {selected} < min {mn}; "
                f"options: {[o.get('name') for o in grp.get('options', [])][:20]}")
        if mx is not None and selected > mx:
            warnings.append(f"group {gi} {grp.get('title')!r}: selected {selected} "
                            f"> max {mx}")
    no = []
    for t in tops.values():
        e = {"id": t["id"], "name": t["name"], "quantity": t["quantity"]}
        kids = []
        for cid, crow in t["children"].items():
            kids.append({"id": cid, "name": crow.get("name"),
                         "quantity": int(crow.get("quantity", 0))})
        if kids:
            e["options"] = kids
        no.append(e)
    return no, warnings


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv=None):
    """Dispatch a subcommand.

    Args:
        argv: Override for sys.argv[1:] (for tests); None = use sys.argv.
    """
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0 if argv else 2)
    sub, rest = argv[0], argv[1:]
    fn = {"reorder": cmd_reorder, "read-options": cmd_read_options,
          "write-options": cmd_write_options, "carts": cmd_carts}.get(sub)
    if not fn:
        print(__doc__)
        sys.exit(2)
    sys.exit(fn(rest))


if __name__ == "__main__":
    main()
