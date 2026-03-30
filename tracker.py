"""
tracker.py — Cursor per-repo activity & cost tracker.

Run on demand:
    python tracker.py                  # sync + report for all time
    python tracker.py --last 30        # sync + report last 30 days
    python tracker.py --since 2026-01  # sync + report from January 2026

Every run syncs new/updated conversations from Cursor's local SQLite databases
into history.db (an append-only local store), then generates the report from
history.db.  This preserves data even if you delete chats in Cursor.

Writes cursor-usage.json in the same directory.

Attribution layers (in priority order):
  1. ai-code-tracking.db  ai_code_hashes  — file paths per composer request  (most accurate)
  2. workspaceStorage      composer data   — conversation → workspace folder mapping
  3. state.vscdb          bubbleId blobs  — relevantFiles / recentlyViewedFiles fallback
  4. watcher.jsonl        time-log        — which repo was open at what time
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

# ── paths ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent
PRICES_FILE = SCRIPT_DIR / "prices.json"
SESSION_FILE = SCRIPT_DIR / "cursor-session.txt"
WATCHER_LOG = SCRIPT_DIR / "watcher.jsonl"
OUTPUT_FILE = SCRIPT_DIR / "cursor-usage.json"
HISTORY_DB  = SCRIPT_DIR / "history.db"

AT_DB = Path(os.environ.get("USERPROFILE", "")) / ".cursor" / "ai-tracking" / "ai-code-tracking.db"
STATE_DB = Path(os.environ.get("APPDATA", "")) / "Cursor" / "User" / "globalStorage" / "state.vscdb"
WS_STORAGE = Path(os.environ.get("APPDATA", "")) / "Cursor" / "User" / "workspaceStorage"


# ── helpers ───────────────────────────────────────────────────────────────────

def find_git_root(path_str: str) -> str | None:
    """
    Given a file or directory path, walk upward until a .git directory is
    found.  Returns the directory containing .git as a normalised string, or
    None if not found.
    """
    try:
        p = _normalise_path(path_str)
        if not p:
            return None
        cur = p if p.is_dir() else p.parent
        visited = set()
        while cur != cur.parent:
            key = str(cur).lower()
            if key in visited:
                break
            visited.add(key)
            if (cur / ".git").exists():
                return str(cur)
            cur = cur.parent
    except Exception:
        pass
    return None


def _normalise_path(raw: str) -> Path | None:
    """
    Convert the odd path formats Cursor stores (/c:/Users/... or C:/Users/...)
    into a proper Path object.
    """
    try:
        s = raw.replace("\\", "/")
        # /c:/Users/... → c:/Users/...
        if s.startswith("/") and len(s) > 2 and s[2] == ":":
            s = s[1:]
        if len(s) >= 2 and s[1] == ":":
            s = s[0].upper() + s[1:]
        return Path(s)
    except Exception:
        return None


def decode_vscode_uri(uri: str) -> Path | None:
    try:
        s = urllib.parse.unquote(uri)
        for prefix in ("file:///", "file://"):
            if s.startswith(prefix):
                s = s[len(prefix):]
                break
        if len(s) >= 2 and s[1] == ":":
            s = s[0].upper() + s[1:]
        return Path(s)
    except Exception:
        return None


def ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def repo_key(path: str) -> str:
    """Normalised lowercase path used as dict key to merge case variants."""
    return path.lower().replace("\\", "/").rstrip("/")


# ── load prices ───────────────────────────────────────────────────────────────

def load_prices() -> dict:
    if PRICES_FILE.exists():
        try:
            return json.loads(PRICES_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[warn] Could not read prices.json: {exc}")
    return {}


def _pricing_slug(name: str) -> str:
    """Normalise a display name or prices.json key to a comparison slug."""
    s = name.lower().strip()
    if s.startswith("the "):
        s = s[4:]
    s = re.sub(r"[\s.]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def _parse_dollar(cell: str) -> float | None:
    """
    Extract a dollar amount from a string.
    Handles '$1.25', '$$1.25' (RSC-escaped), and bare '1.25'.
    """
    s = cell.replace(",", "").strip().lstrip("$")
    # Try bare number first (RSC strips the $ when we lstrip)
    m = re.match(r"^(\d+\.?\d*)$", s)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    # Also try with embedded $ sign (original HTML table path, kept for safety)
    m2 = re.search(r"\$([\d]+\.?\d*)", cell.replace(",", ""))
    if m2:
        try:
            return float(m2.group(1))
        except ValueError:
            pass
    return None


def _rsc_tr_rows(rsc_section: str) -> list[list[str]]:
    """
    Extract text-content rows from RSC tr elements in the given text section.

    RSC element format: ["$","tr",null,{"children":[["$","td",null,{"children":"text"}],...]}]
    Uses bracket counting to find each tr's exact boundary so adjacent rows
    don't bleed into each other.
    """
    rows: list[list[str]] = []
    pos = 0
    while True:
        start = rsc_section.find('["$","tr"', pos)
        if start < 0:
            break
        # Walk forward counting brackets to find the end of this tr element
        depth = 0
        end = start
        for i, ch in enumerate(rsc_section[start:], start):
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        tr_src = rsc_section[start:end]
        # Pull out "children":"<text>" values (skips nested arrays/objects)
        texts = re.findall(r'"children":"([^"]+)"', tr_src)
        # RSC escapes a literal $ as $$ — strip the leading $ prefix
        texts = [t.lstrip("$") for t in texts if t]
        if texts:
            rows.append(texts)
        pos = max(end, start + 1)
    return rows


def _pw_extract_tables(page) -> list[list[list[str]]]:  # type: ignore[no-untyped-def]
    """Extract all <table> elements from a Playwright page as list-of-rows."""
    tables: list[list[list[str]]] = []
    for tbl in page.query_selector_all("table"):
        rows: list[list[str]] = []
        for tr in tbl.query_selector_all("tr"):
            cells = [td.inner_text().strip() for td in tr.query_selector_all("td,th")]
            if any(cells):
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables


def _fetch_pricing_playwright() -> dict | None:
    """
    Use a headless Chromium browser (Playwright) to fully render
    cursor.com/docs/models-and-pricing and extract ALL pricing tables.

    Table structure on the current page:
      - Table 0: "Token type" | "Price per 1M tokens"  (auto pool rates)
      - Table 1: "Name" | "Input" | "Cache Write" | "Cache Read" | "Output"  (Composer)
      - Table 2: same header — premium routing models
      - Table 3: Plans (ignored)

    Returns {"auto_pool": {input, output}, "models": {slug: {input, output}}}
    or None on failure.
    """
    from playwright.sync_api import sync_playwright  # type: ignore[import]

    url = "https://cursor.com/docs/models-and-pricing"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(url, wait_until="networkidle", timeout=30000)
            page.wait_for_selector("table", timeout=15000)
            page.wait_for_timeout(500)

            # Click "Show more models" if present (expands the full model pricing table)
            try:
                show_more = page.get_by_role("button", name="Show more models")
                if show_more.count() > 0:
                    show_more.first.click()
                    page.wait_for_timeout(1500)
            except Exception:
                pass

            # Scroll the main content area to ensure all model rows are rendered
            page.evaluate("""() => {
                const main = document.getElementById('main-content') ||
                             document.querySelector('main') ||
                             document.body;
                main.scrollTo(0, main.scrollHeight);
            }""")
            page.wait_for_timeout(500)

            tables = _pw_extract_tables(page)
            browser.close()
    except Exception as exc:
        print(f"[warn] Playwright fetch failed: {exc}")
        return None

    auto_pool: dict | None = None
    models: dict[str, dict] = {}

    for tbl_rows in tables:
        if not tbl_rows:
            continue
        header = [c.lower() for c in tbl_rows[0]]

        # Auto pool table: 2 columns — "Token type" | "Price per 1M tokens"
        # Row labels contain "input"/"output"; columns do NOT.
        if any("token" in h for h in header) and any("price" in h or "per 1m" in h for h in header):
            price_idx = next((i for i, h in enumerate(header) if "price" in h or "per 1m" in h), 1)
            for row in tbl_rows[1:]:
                if len(row) <= price_idx:
                    continue
                label = row[0].lower()
                val = _parse_dollar(row[price_idx])
                if val is None:
                    continue
                if ("input" in label or "cache write" in label) and "cache read" not in label:
                    auto_pool = auto_pool or {}
                    auto_pool.setdefault("input", val)
                elif "output" in label and "cache" not in label:
                    auto_pool = auto_pool or {}
                    auto_pool.setdefault("output", val)
            continue

        # Model pricing tables: "Name" | "Input" | ... | "Output"
        name_idx = next((i for i, h in enumerate(header) if "name" in h or "model" in h), None)
        inp_idx = next((i for i, h in enumerate(header) if h == "input" or h.startswith("input ")), None)
        out_idx = next((i for i, h in enumerate(header) if h == "output" or h.startswith("output ")), None)

        if name_idx is None or inp_idx is None or out_idx is None:
            continue

        for row in tbl_rows[1:]:
            if len(row) <= max(name_idx, inp_idx, out_idx):
                continue
            name = row[name_idx].strip()
            if not name or name.lower() == "name":
                continue
            inp = _parse_dollar(row[inp_idx])
            out = _parse_dollar(row[out_idx])
            if inp is not None and out is not None:
                models[_pricing_slug(name)] = {"input": inp, "output": out}

    if auto_pool is None and not models:
        print("[warn] Playwright: no pricing data found in rendered tables")
        return None

    return {"auto_pool": auto_pool, "models": models}


def _fetch_pricing_rsc() -> dict | None:
    """
    Fallback pricing fetch using the raw RSC stream (no JS execution).

    The cursor.com docs page is Next.js App Router — the auto pool rates are
    server-rendered in the __next_f.push() RSC payload and can be extracted
    without a browser.  Model-specific rates live in a lazy client component
    and are NOT available via this method; only auto_pool is returned.

    Returns {"auto_pool": {input, output}, "models": {}} or None on failure.
    """
    url = "https://cursor.com/docs/models-and-pricing"
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; cursortrack/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            html_src = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        print(f"[warn] Could not fetch cursor pricing page: {exc}")
        return None

    rsc_text = ""
    for chunk in re.findall(r'self\.__next_f\.push\(\[(.*?)\]\)', html_src, re.DOTALL):
        m = re.match(r'\d+,"(.*)"$', chunk, re.DOTALL)
        if m:
            try:
                rsc_text += json.loads('"' + m.group(1) + '"')
            except Exception:
                rsc_text += m.group(1)

    if not rsc_text:
        print("[warn] No RSC stream found on cursor pricing page")
        return None

    auto_section_start = rsc_text.lower().find("auto pricing")
    if auto_section_start < 0:
        print("[warn] 'Auto pricing' heading not found in RSC stream")
        return None

    section = rsc_text[auto_section_start: auto_section_start + 5000]
    rows = _rsc_tr_rows(section)

    auto_input: float | None = None
    auto_output: float | None = None

    for row in rows:
        joined = " ".join(row).lower()
        if "input" in joined or "cache write" in joined:
            for cell in row:
                v = _parse_dollar(cell)
                if v is not None:
                    auto_input = v
                    break
        elif "output" in joined and "cache" not in joined:
            for cell in row:
                v = _parse_dollar(cell)
                if v is not None:
                    auto_output = v
                    break

    if auto_input is None or auto_output is None:
        print(
            f"[warn] Could not extract auto pool rates from RSC stream "
            f"(input={auto_input}, output={auto_output})"
        )
        return None

    return {"auto_pool": {"input": auto_input, "output": auto_output}, "models": {}}


def fetch_cursor_pricing() -> dict | None:
    """
    Fetch pricing from cursor.com/docs/models-and-pricing.

    Tries Playwright first (full model table + auto pool rates).
    Falls back to RSC stream parsing (auto pool only) if Playwright is not installed.

    Returns {"auto_pool": {input, output}, "models": {slug: {input, output}}}
    or None on complete failure.
    """
    try:
        from playwright.sync_api import sync_playwright as _pw_check  # noqa: F401
        return _fetch_pricing_playwright()
    except ImportError:
        print("[warn] playwright not installed — falling back to RSC-only fetch (auto pool rates only)")
        return _fetch_pricing_rsc()


def update_prices_from_cursor(fetched: dict, prices: dict) -> tuple[dict, bool]:
    """
    Merge freshly-fetched Cursor pricing into the prices dict (in-place copy).

    - Updates "default" from auto_pool rates if they changed.
    - For each non-underscore key in prices: normalise → prefix-match against
      fetched model slugs → update input/output if they differ.
    - Always writes _last_price_fetch; only writes _last_price_update when a
      rate actually changes.

    Returns (updated_prices, changed: bool).
    """
    prices = dict(prices)  # shallow copy so caller's dict is untouched
    changed = False
    now_iso = datetime.now(timezone.utc).isoformat()

    # "default" ← auto pool rates
    auto_pool = fetched.get("auto_pool")
    if auto_pool:
        existing = prices.get("default", {})
        if (existing.get("input") != auto_pool["input"] or
                existing.get("output") != auto_pool["output"]):
            prices["default"] = {"input": auto_pool["input"], "output": auto_pool["output"]}
            changed = True

    # Per-model keys — update existing entries and insert new ones
    fetched_models = fetched.get("models", {})

    # Build a set of fetched slugs already matched to existing keys (to avoid double-inserting)
    matched_fetched: set[str] = set()

    for key, val in list(prices.items()):
        if key.startswith("_") or not isinstance(val, dict) or key == "default":
            continue
        key_slug = _pricing_slug(key)
        for fslug, frates in fetched_models.items():
            if key_slug.startswith(fslug) or fslug.startswith(key_slug):
                matched_fetched.add(fslug)
                if (val.get("input") != frates["input"] or
                        val.get("output") != frates["output"]):
                    prices[key] = {"input": frates["input"], "output": frates["output"]}
                    changed = True
                break

    # Insert completely new model keys (slugs not matched to any existing key)
    for fslug, frates in fetched_models.items():
        if fslug not in matched_fetched:
            prices[fslug] = {"input": frates["input"], "output": frates["output"]}
            changed = True

    prices["_last_price_fetch"] = now_iso
    if changed:
        prices["_last_price_update"] = now_iso

    return prices, changed


def get_rates(model: str, prices: dict) -> tuple[float, float]:
    """
    Return (input_rate, output_rate) in $/1M tokens for a given model.

    Uses per-model rates from prices.json keyed by model name.
    Cursor's Auto mode stores 'default' as the model name — the 'default'
    key in prices.json covers those requests.
    """
    info = prices.get(model) or prices.get("default") or {}
    return info.get("input", 0.0), info.get("output", 0.0)


def avg_output_tokens(prices: dict) -> int:
    """Estimated output tokens per request when Cursor didn't store the actual count."""
    return int(prices.get("_avg_output_tokens", 2000))


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    requests: int,
    prices: dict,
    has_real_input: bool = False,
) -> tuple[float, bool]:
    """
    Returns (estimated_usd, is_estimated).

    Input tokens: actual values from contextWindowStatusAtCreation.
    Output tokens: delta-method estimate (context growth between consecutive
                   requests), with flat fallback when only one request exists.

    Returns (0.0, False) when no token data is available at all.
    """
    rate_in, rate_out = get_rates(model, prices)
    if not has_real_input or input_tokens == 0:
        return 0.0, False

    # If we got no delta-based output estimate (e.g. single-request session),
    # fall back to the configured flat estimate per request.
    eff_out = output_tokens if output_tokens > 0 else requests * avg_output_tokens(prices)
    cost = (input_tokens / 1_000_000 * rate_in) + (eff_out / 1_000_000 * rate_out)
    return cost, True


# ── token data from state.vscdb bubbles ──────────────────────────────────────

def read_bubble_tokens() -> dict[str, dict]:
    """
    Scan all bubbleId entries in state.vscdb for token data.

    Input tokens: read from contextWindowStatusAtCreation.tokensUsed on each
    TYPE-1 (user) bubble — this is the actual context window size sent to the
    model for that request.

    Output tokens: estimated via the DELTA method.  Within each conversation,
    consecutive type-1 bubbles are sorted by timestamp.  The growth in
    tokensUsed between request N and request N+1 approximates the output
    generated by request N (response text + thinking tokens + any new file
    context added by the agent).  Negative deltas (context window compression)
    are discarded.  The user's own message tokens (~text_len/4) are subtracted
    from each delta to avoid double-counting.

    Returns:
        {
          "by_request": { requestId -> {"input_tokens": N, "model": "...", "conv_id": "..."} },
          "by_conv":    { conversationId -> {"input_tokens": N, "output_tokens": N, "model": "..."} },
        }
    """
    if not STATE_DB.exists():
        return {"by_request": {}, "by_conv": {}}

    try:
        con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"
        ).fetchall()
        con.close()
    except Exception as exc:
        print(f"[warn] Could not read state.vscdb for token data: {exc}")
        return {"by_request": {}, "by_conv": {}}

    by_request: dict[str, dict] = {}
    # Store per-conv sequences for delta computation
    conv_sequences: dict[str, list] = defaultdict(list)

    for row in rows:
        parts = row["key"].split(":")
        if len(parts) < 3:
            continue
        conv_id = parts[1]
        try:
            blob = json.loads(row["value"])
        except Exception:
            continue

        if blob.get("type") != 1:
            continue

        cws = blob.get("contextWindowStatusAtCreation")
        if not isinstance(cws, dict):
            continue

        tokens_used = cws.get("tokensUsed", 0)
        if not tokens_used:
            continue

        model = ""
        mi = blob.get("modelInfo")
        if isinstance(mi, dict):
            model = mi.get("modelName", "")

        req_id = blob.get("requestId")
        created_at = blob.get("createdAt") or ""
        user_text_len = len(blob.get("text") or "")

        # Parse ISO timestamp to ms epoch for storage
        created_at_ms: int | None = None
        if created_at:
            try:
                dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
                created_at_ms = int(dt.timestamp() * 1000)
            except Exception:
                pass

        if req_id:
            by_request[req_id] = {
                "input_tokens": tokens_used,
                "model": model,
                "conv_id": conv_id,
                "created_at_ms": created_at_ms,
            }

        conv_sequences[conv_id].append({
            "ts": created_at,
            "tokens": tokens_used,
            "user_text_len": user_text_len,
            "model": model,
            "req_id": req_id,
        })

    # Compute per-conversation totals using the delta method for output
    by_conv: dict[str, dict] = {}
    for conv_id, seq in conv_sequences.items():
        seq.sort(key=lambda x: x["ts"])
        total_input = sum(s["tokens"] for s in seq)
        total_output = 0
        model = ""
        for i, s in enumerate(seq):
            if s["model"]:
                model = s["model"]
            if i + 1 < len(seq):
                delta = seq[i + 1]["tokens"] - s["tokens"]
                if delta > 0:
                    # Subtract rough user-message tokens from the next request
                    user_msg_tokens = seq[i + 1]["user_text_len"] // 4
                    output_est = max(0, delta - user_msg_tokens)
                    total_output += output_est
                # Negative delta = context compression; skip (we lose that output estimate)

        by_conv[conv_id] = {
            "input_tokens": total_input,
            "output_tokens": total_output,
            "model": model,
        }

    return {"by_request": by_request, "by_conv": by_conv}


# ── layer 1: ai-code-tracking.db ─────────────────────────────────────────────

def read_ai_tracking(since_ts_ms: int | None, bubble_tokens: dict) -> list[dict]:
    """
    Returns one dict per attributed conversation with:
        conversationId, repo_path, model, requests, files,
        input_tokens, first_ts, last_ts, layer
    """
    if not AT_DB.exists():
        print(f"[warn] ai-code-tracking.db not found at {AT_DB}")
        return []

    try:
        con = sqlite3.connect(f"file:{AT_DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row

        # Fetch per-request rows so we can look up token counts by requestId
        query = """
            SELECT
                conversationId,
                fileName,
                requestId,
                model,
                timestamp
            FROM ai_code_hashes
            WHERE conversationId IS NOT NULL
              AND fileName IS NOT NULL
              AND source = 'composer'
        """
        params: list = []
        if since_ts_ms:
            query += " AND timestamp >= ?"
            params.append(since_ts_ms)
        query += " ORDER BY timestamp"

        rows = con.execute(query, params).fetchall()
        con.close()
    except Exception as exc:
        print(f"[warn] Could not read ai-code-tracking.db: {exc}")
        return []

    by_req = bubble_tokens.get("by_request", {})
    by_conv_tokens = bubble_tokens.get("by_conv", {})

    conv_files: dict[str, dict] = defaultdict(lambda: {
        "roots": defaultdict(int),
        "model": None,
        "request_ids": set(),
        "files": 0,
        "first_ts": None,
        "last_ts": None,
    })

    for row in rows:
        cid = row["conversationId"]
        fn = row["fileName"]
        root = find_git_root(fn)
        if root is None:
            root = "__unattributed__"

        d = conv_files[cid]
        d["roots"][repo_key(root)] += 1
        d["model"] = row["model"] or d["model"]
        if row["requestId"]:
            d["request_ids"].add(row["requestId"])
        d["files"] += 1
        ts = row["timestamp"]
        if d["first_ts"] is None or (ts and ts < d["first_ts"]):
            d["first_ts"] = ts
        if d["last_ts"] is None or (ts and ts > d["last_ts"]):
            d["last_ts"] = ts

    results = []
    for cid, d in conv_files.items():
        dominant_key = max(d["roots"], key=d["roots"].__getitem__)
        real_root = _find_real_root(dominant_key) or dominant_key

        # Input tokens: sum contextWindowStatus per request; fall back to conv total
        input_tokens = sum(
            by_req[rid]["input_tokens"]
            for rid in d["request_ids"]
            if rid in by_req
        )
        conv_tok = by_conv_tokens.get(cid, {})
        if not input_tokens:
            input_tokens = conv_tok.get("input_tokens", 0)

        # Output tokens: from delta method stored at conv level
        output_tokens = conv_tok.get("output_tokens", 0)

        results.append({
            "conversationId": cid,
            "repo_path": real_root,
            "model": d["model"] or "unknown",
            "requests": len(d["request_ids"]) or 1,
            "files": d["files"],
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "has_real_input": input_tokens > 0,
            "first_ts": d["first_ts"],
            "last_ts": d["last_ts"],
            "layer": "commit-linked",
        })

    return results


_REAL_ROOT_CACHE: dict[str, str] = {}

def _find_real_root(key: str) -> str | None:
    """Return the real (properly-cased) path for a lowercased root key."""
    if key in _REAL_ROOT_CACHE:
        return _REAL_ROOT_CACHE[key]
    if key == "__unattributed__":
        return None
    # Try to resolve to a real path
    p = Path(key)
    if p.exists():
        _REAL_ROOT_CACHE[key] = str(p)
        return str(p)
    # Capitalise drive letter (Windows common case)
    if len(key) >= 2 and key[1] == ":":
        p2 = Path(key[0].upper() + key[1:])
        if p2.exists():
            _REAL_ROOT_CACHE[key] = str(p2)
            return str(p2)
    return None


# ── layer 1b: workspace SQLite attribution ────────────────────────────────────

def read_workspace_attribution() -> dict[str, str]:
    """
    Scan every workspaceStorage/*/state.vscdb.

    Each workspace stores a composer.composerData.allComposers list whose
    composerId values are the same conversation IDs used in bubbleId keys and
    ai_code_hashes.conversationId.  Paired with the folder path from
    workspace.json, this gives an exact conversationId → git_root mapping for
    all conversations that were opened in a single-folder workspace — no live
    watcher required.

    For multi-root workspaces (workspace.json points to a .code-workspace file
    rather than a folder), the mapping is skipped since the root is ambiguous.

    Returns: { conversationId -> git_root_path }
    """
    if not WS_STORAGE.exists():
        return {}

    result: dict[str, str] = {}

    for ws_dir in WS_STORAGE.iterdir():
        if not ws_dir.is_dir():
            continue

        # Resolve workspace folder from workspace.json
        wj = ws_dir / "workspace.json"
        folder: Path | None = None
        if wj.exists():
            try:
                data = json.loads(wj.read_text(encoding="utf-8"))
                uri = data.get("folder")  # present only for single-folder workspaces
                if uri:
                    folder = decode_vscode_uri(uri)
            except Exception:
                pass

        if folder is None:
            continue  # multi-root or missing workspace.json — skip

        root = find_git_root(str(folder))
        if root is None:
            root = str(folder)  # not a git repo, use folder itself

        ws_db = ws_dir / "state.vscdb"
        if not ws_db.exists():
            continue

        try:
            con = sqlite3.connect(f"file:{ws_db}?mode=ro", uri=True)
            row = con.execute(
                "SELECT value FROM ItemTable WHERE key = 'composer.composerData'"
            ).fetchone()
            con.close()
        except Exception:
            continue

        if not row or not row[0]:
            continue

        try:
            data = json.loads(row[0])
            for comp in data.get("allComposers", []):
                cid = comp.get("composerId")
                if cid and cid not in result:
                    result[cid] = root
        except Exception:
            continue

    return result


# ── layer 2: state.vscdb bubbles ─────────────────────────────────────────────

def read_bubbles(
    since_ts_ms: int | None,
    known_conv_ids: set[str],
    bubble_tokens: dict,
    workspace_attr: dict[str, str],
) -> list[dict]:
    """
    Scan bubbleId entries in state.vscdb.  For each conversation NOT already
    covered by Layer 1, attempt attribution via relevantFiles /
    recentlyViewedFiles and collect token counts (if available).

    Returns list of dicts with the same shape as read_ai_tracking().
    """
    if not STATE_DB.exists():
        print(f"[warn] state.vscdb not found at {STATE_DB}")
        return []

    try:
        con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"
        ).fetchall()
        con.close()
    except Exception as exc:
        print(f"[warn] Could not read state.vscdb bubbles: {exc}")
        return []

    # conversationId is the second UUID segment in "bubbleId:<cid>:<bid>"
    conv_data: dict[str, dict] = defaultdict(lambda: {
        "roots": defaultdict(int),
        "model": None,
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "has_real_input": False,
        "first_ts": None,
        "last_ts": None,
    })

    for row in rows:
        parts = row["key"].split(":")
        if len(parts) < 3:
            continue
        cid = parts[1]
        if cid in known_conv_ids:
            continue  # already attributed by Layer 1

        try:
            blob = json.loads(row["value"])
        except Exception:
            continue

        created_at = blob.get("createdAt")
        if created_at is not None:
            try:
                created_at = int(created_at)
            except (TypeError, ValueError):
                created_at = None
        if since_ts_ms and created_at and created_at < since_ts_ms:
            continue

        d = conv_data[cid]

        # Token counts from tokenCount field (often 0)
        tc = blob.get("tokenCount") or {}
        if isinstance(tc, dict):
            d["input_tokens"] += tc.get("inputTokens", 0)
            d["output_tokens"] += tc.get("outputTokens", 0)

        # contextWindowStatusAtCreation.tokensUsed on type-1 bubbles (the real input count)
        if blob.get("type") == 1:
            cws = blob.get("contextWindowStatusAtCreation")
            if isinstance(cws, dict) and cws.get("tokensUsed", 0) > 0:
                d["input_tokens"] += cws["tokensUsed"]
                d["has_real_input"] = True

        # Model
        mi = blob.get("modelInfo") or {}
        if isinstance(mi, dict) and (mi.get("modelName") or mi.get("name")):
            d["model"] = mi.get("modelName") or mi.get("name")

        # Timestamps
        if created_at:
            if d["first_ts"] is None or created_at < d["first_ts"]:
                d["first_ts"] = created_at
            if d["last_ts"] is None or created_at > d["last_ts"]:
                d["last_ts"] = created_at

        # Attribution priority:
        # 1. workspace SQLite (exact: composer → workspace folder)
        if cid in workspace_attr:
            ws_root = workspace_attr[cid]
            d["roots"][repo_key(ws_root)] += 10  # weight > file hits

        # 2. relevantFiles / recentlyViewedFiles in the bubble
        files = []
        for key in ("relevantFiles", "recentlyViewedFiles"):
            val = blob.get(key)
            if isinstance(val, list):
                files.extend(val)

        for f in files:
            if not isinstance(f, str):
                continue
            root = find_git_root(f)
            if root:
                d["roots"][repo_key(root)] += 1

        # Count type-2 (assistant) bubbles as "requests"
        if blob.get("type") == 2:
            d["requests"] += 1

    # Also pull in conv-level token data from bubble_tokens for any conv not yet seen
    by_conv_tokens = bubble_tokens.get("by_conv", {})
    for cid in list(conv_data.keys()):
        if not conv_data[cid]["has_real_input"] and cid in by_conv_tokens:
            conv_data[cid]["input_tokens"] += by_conv_tokens[cid]["input_tokens"]
            if conv_data[cid]["input_tokens"] > 0:
                conv_data[cid]["has_real_input"] = True
            if not conv_data[cid]["model"] and by_conv_tokens[cid].get("model"):
                conv_data[cid]["model"] = by_conv_tokens[cid]["model"]

    results = []
    for cid, d in conv_data.items():
        if d["roots"]:
            dominant_key = max(d["roots"], key=d["roots"].__getitem__)
            real_root = _find_real_root(dominant_key) or dominant_key
            # Determine which attribution source won
            if cid in workspace_attr and repo_key(workspace_attr[cid]) == dominant_key:
                layer = "workspace-sqlite"
            else:
                layer = "bubble-context"
        else:
            real_root = "__unattributed__"
            layer = "bubble-no-files"

        results.append({
            "conversationId": cid,
            "repo_path": real_root,
            "model": d["model"] or "unknown",
            "requests": d["requests"],
            "files": 0,
            "input_tokens": d["input_tokens"],
            "output_tokens": d["output_tokens"],
            "has_real_input": d["has_real_input"],
            "first_ts": d["first_ts"],
            "last_ts": d["last_ts"],
            "layer": layer,
        })

    return results


# ── layer 3: watcher.jsonl ────────────────────────────────────────────────────

def read_watcher_log(since_ts: int | None) -> list[dict]:
    """Load watcher entries newer than since_ts (unix seconds)."""
    if not WATCHER_LOG.exists():
        return []
    entries = []
    with open(WATCHER_LOG, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                if since_ts and e.get("ts", 0) < since_ts:
                    continue
                entries.append(e)
            except Exception:
                continue
    return entries


def watcher_repo_at(ts_ms: int, watcher_entries: list[dict]) -> str | None:
    """
    Given a timestamp (ms), return the repo that the watcher recorded nearest
    before that time.
    """
    ts_s = ts_ms / 1000
    best = None
    for e in watcher_entries:
        ets = e.get("ts", 0)
        if ets <= ts_s:
            best = e.get("repo")
        else:
            break  # entries are in chronological order
    return best


# ── cursor usage API ──────────────────────────────────────────────────────────

def load_session_cookie() -> str | None:
    """
    Read the Cursor browser session cookie from cursor-session.txt.
    The file should contain the full Cookie header value on a single line.
    """
    if SESSION_FILE.exists():
        try:
            return SESSION_FILE.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return None


def fetch_usage_api(month: str, cookie: str | None) -> dict | None:
    """
    Fetch https://www.cursor.com/api/usage?month=YYYY-MM using the browser
    session cookie.  Returns the parsed JSON or None on failure.
    """
    if not cookie:
        return None
    url = f"https://www.cursor.com/api/usage?month={month}"
    try:
        req = urllib.request.Request(
            url,
            headers={
                "Cookie": cookie,
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json",
                "Referer": "https://www.cursor.com/settings",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        print(f"[warn] Usage API failed for {month}: {exc}")
        return None


def fetch_subscription_info(cookie: str | None) -> dict:
    """Read subscription type and email from state.vscdb ItemTable."""
    info: dict = {}
    if STATE_DB.exists():
        try:
            con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
            rows = con.execute(
                "SELECT key, value FROM ItemTable WHERE key LIKE 'cursorAuth/%'"
            ).fetchall()
            con.close()
            mapping = {r[0]: r[1] for r in rows}
            info["email"] = mapping.get("cursorAuth/cachedEmail", "")
            info["plan"] = mapping.get("cursorAuth/stripeMembershipType", "")
            info["subscription_status"] = mapping.get("cursorAuth/stripeSubscriptionStatus", "")
        except Exception:
            pass
    return info


# ── history.db persistence ───────────────────────────────────────────────────

def init_history_db() -> sqlite3.Connection:
    """
    Open (or create) history.db and ensure the schema exists.
    Returns an open read-write connection.
    """
    con = sqlite3.connect(str(HISTORY_DB))
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE IF NOT EXISTS conversations (
            conv_id            TEXT PRIMARY KEY,
            repo_path          TEXT,
            layer              TEXT,
            model              TEXT,
            first_ts           INTEGER,
            last_ts            INTEGER,
            requests           INTEGER,
            files_edited       INTEGER,
            input_tokens       INTEGER,
            output_tokens      INTEGER,
            has_real_input     INTEGER,
            estimated_cost_usd REAL,
            synced_at          INTEGER
        );
        CREATE TABLE IF NOT EXISTS requests (
            request_id         TEXT PRIMARY KEY,
            conv_id            TEXT,
            model              TEXT,
            created_at         INTEGER,
            input_tokens       INTEGER,
            output_tokens      INTEGER,
            estimated_cost_usd REAL
        );
        CREATE INDEX IF NOT EXISTS idx_conv_last_ts  ON conversations(last_ts);
        CREATE INDEX IF NOT EXISTS idx_conv_repo      ON conversations(repo_path);
        CREATE INDEX IF NOT EXISTS idx_req_conv_id   ON requests(conv_id);
        CREATE INDEX IF NOT EXISTS idx_req_created   ON requests(created_at);
    """)
    return con


def sync_to_history(all_convs: list[dict], bubble_tokens: dict, prices: dict) -> tuple[int, int]:
    """
    Upsert conversations (and individual requests) into history.db.

    A conversation is upserted when:
      - it is not yet in history.db, OR
      - its input_tokens or output_tokens have increased since last sync
        (conversations can accumulate tokens as a session progresses).

    Returns (new_count, updated_count).
    """
    con = init_history_db()
    now_ms = int(time.time() * 1000)

    # Load existing snapshots for comparison
    existing = {
        row["conv_id"]: (row["input_tokens"] or 0, row["output_tokens"] or 0)
        for row in con.execute("SELECT conv_id, input_tokens, output_tokens FROM conversations")
    }

    new_count = updated_count = 0

    for conv in all_convs:
        cid = conv.get("conversationId") or conv.get("conv_id")
        if not cid:
            continue

        inp = conv.get("input_tokens", 0) or 0
        out = conv.get("output_tokens", 0) or 0
        model = conv.get("model", "unknown") or "unknown"
        requests = conv.get("requests", 0) or 0

        # Effective output: delta if available, else flat estimate
        eff_out = out if out > 0 else requests * avg_output_tokens(prices)
        cost, _ = estimate_cost(
            model, inp, eff_out, requests, prices,
            has_real_input=bool(conv.get("has_real_input")),
        )

        if cid in existing:
            old_inp, old_out = existing[cid]
            if inp <= old_inp and out <= old_out:
                continue  # no change — skip
            updated_count += 1
        else:
            new_count += 1

        con.execute(
            """
            INSERT INTO conversations
                (conv_id, repo_path, layer, model, first_ts, last_ts,
                 requests, files_edited, input_tokens, output_tokens,
                 has_real_input, estimated_cost_usd, synced_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(conv_id) DO UPDATE SET
                repo_path          = excluded.repo_path,
                layer              = excluded.layer,
                model              = excluded.model,
                first_ts           = excluded.first_ts,
                last_ts            = excluded.last_ts,
                requests           = excluded.requests,
                files_edited       = excluded.files_edited,
                input_tokens       = excluded.input_tokens,
                output_tokens      = excluded.output_tokens,
                has_real_input     = excluded.has_real_input,
                estimated_cost_usd = excluded.estimated_cost_usd,
                synced_at          = excluded.synced_at
            """,
            (
                cid,
                conv.get("repo_path"),
                conv.get("layer"),
                model,
                conv.get("first_ts"),
                conv.get("last_ts"),
                requests,
                conv.get("files", 0) or 0,
                inp,
                out,
                1 if conv.get("has_real_input") else 0,
                round(cost, 6),
                now_ms,
            ),
        )

    # Sync per-request rows from bubble_tokens
    by_req = bubble_tokens.get("by_request", {})
    existing_reqs = {
        row[0] for row in con.execute("SELECT request_id FROM requests")
    }
    for req_id, rdata in by_req.items():
        if req_id in existing_reqs:
            continue
        r_inp = rdata.get("input_tokens", 0) or 0
        r_model = rdata.get("model", "unknown") or "unknown"
        r_cost, _ = estimate_cost(r_model, r_inp, 0, 1, prices, has_real_input=r_inp > 0)
        con.execute(
            """
            INSERT OR IGNORE INTO requests
                (request_id, conv_id, model, created_at, input_tokens, output_tokens, estimated_cost_usd)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                req_id,
                rdata.get("conv_id"),
                r_model,
                rdata.get("created_at_ms"),
                r_inp,
                0,
                round(r_cost, 6),
            ),
        )

    con.commit()
    con.close()
    return new_count, updated_count


def build_report_from_history(
    since_ts_ms: int | None,
    since_label: str,
    prices: dict,
    usage_api: dict | None,
    until_ts_ms: int | None = None,
) -> dict:
    """
    Generate the cost report by reading from history.db rather than live
    Cursor sources, so previously deleted chats are included.

    since_ts_ms : lower bound — conversations whose last_ts >= since_ts_ms.
    until_ts_ms : upper bound — conversations whose first_ts <= until_ts_ms.
    Either may be None (= no bound on that side).
    """
    con = init_history_db()

    if since_ts_ms and until_ts_ms:
        rows = con.execute(
            """
            SELECT * FROM conversations
            WHERE (last_ts >= ? OR (last_ts IS NULL AND first_ts >= ?))
              AND first_ts <= ?
            """,
            (since_ts_ms, since_ts_ms, until_ts_ms),
        ).fetchall()
    elif since_ts_ms:
        rows = con.execute(
            """
            SELECT * FROM conversations
            WHERE last_ts >= ? OR (last_ts IS NULL AND first_ts >= ?)
            """,
            (since_ts_ms, since_ts_ms),
        ).fetchall()
    elif until_ts_ms:
        rows = con.execute(
            "SELECT * FROM conversations WHERE first_ts <= ?",
            (until_ts_ms,),
        ).fetchall()
    else:
        rows = con.execute("SELECT * FROM conversations").fetchall()

    con.close()

    # Aggregate per repo — same logic as build_report() but from DB rows
    repos: dict[str, dict] = {}

    def get_or_create(path: str) -> dict:
        k = repo_key(path)
        if k not in repos:
            repos[k] = {
                "path": path,
                "conversations": 0,
                "requests": 0,
                "files_edited": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "estimated_cost_usd": 0.0,
                "models": {},
                "first_seen": None,
                "last_seen": None,
                "attribution_layers": set(),
            }
        return repos[k]

    unattributed = {
        "conversations": 0,
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "estimated_cost_usd": 0.0,
    }

    for row in rows:
        rp = row["repo_path"] or "__unattributed__"
        inp  = row["input_tokens"]  or 0
        out  = row["output_tokens"] or 0
        reqs = row["requests"]      or 0
        cost = row["estimated_cost_usd"] or 0.0
        model = row["model"] or "unknown"

        if rp == "__unattributed__":
            unattributed["conversations"] += 1
            unattributed["requests"] += reqs
            unattributed["input_tokens"] += inp
            unattributed["output_tokens"] += out
            unattributed["estimated_cost_usd"] += cost
            continue

        r = get_or_create(rp)
        r["conversations"] += 1
        r["requests"] += reqs
        r["files_edited"] += row["files_edited"] or 0
        r["input_tokens"] += inp
        r["output_tokens"] += out
        r["estimated_cost_usd"] += cost
        r["models"][model] = r["models"].get(model, 0) + 1
        r["attribution_layers"].add(row["layer"] or "unknown")

        ts1 = row["first_ts"]
        ts2 = row["last_ts"]
        if ts1:
            if r["first_seen"] is None or ts1 < r["first_seen"]:
                r["first_seen"] = ts1
        if ts2:
            if r["last_seen"] is None or ts2 > r["last_seen"]:
                r["last_seen"] = ts2

    # Serialise
    repos_out = {}
    for k, r in sorted(repos.items(), key=lambda x: -x[1]["requests"]):
        name = Path(r["path"]).name
        entry = dict(r)
        entry["attribution_layers"] = sorted(entry["attribution_layers"])
        if entry["first_seen"]:
            entry["first_seen"] = ms_to_dt(entry["first_seen"]).isoformat()
        if entry["last_seen"]:
            entry["last_seen"] = ms_to_dt(entry["last_seen"]).isoformat()
        entry["estimated_cost_usd"] = round(entry["estimated_cost_usd"], 4)
        repos_out[name] = entry

    # Usage API summary
    api_summary: dict = {}
    if usage_api:
        for model, stats in usage_api.items():
            if model == "startOfMonth":
                api_summary["_period_start"] = stats
                continue
            if isinstance(stats, dict) and (
                stats.get("numRequests", 0) > 0 or stats.get("numTokens", 0) > 0
            ):
                api_summary[model] = stats

    totals = {
        "conversations": sum(r["conversations"] for r in repos.values()),
        "requests": sum(r["requests"] for r in repos.values()),
        "files_edited": sum(r["files_edited"] for r in repos.values()),
        "input_tokens": sum(r["input_tokens"] for r in repos.values()),
        "output_tokens": sum(r["output_tokens"] for r in repos.values()),
        "estimated_cost_usd": round(
            sum(r["estimated_cost_usd"] for r in repos.values())
            + unattributed["estimated_cost_usd"],
            4,
        ),
        "pricing_mode": "api_direct",
    }

    cost_note = (
        "Rates: per-model API prices from prices.json. "
        "Cursor's Auto mode (stored as 'default') uses the 'default' rate entry. "
        "Input tokens: actual contextWindowStatusAtCreation.tokensUsed from local SQLite — the full context window sent per request. "
        "Output tokens: estimated via delta method (growth in context window between consecutive requests within each conversation). "
        "Negative deltas from context compression are discarded. "
        f"Single-request sessions fall back to a flat estimate of {avg_output_tokens(prices):,} output tokens (configurable via _avg_output_tokens in prices.json). "
        "Thinking-model sessions (claude-*-thinking) include thinking tokens in the output estimate. "
        "Sessions with no local token data show 0 cost. "
        "Data is read from history.db — an append-only local store that persists conversations even after they are deleted in Cursor. "
        "Attribution layers: commit-linked > workspace-sqlite > bubble-context > watcher-fallback."
    )

    # Emit only the model rate entries (skip _meta keys) for the dashboard
    prices_export = {
        k: v for k, v in prices.items()
        if not k.startswith("_") and isinstance(v, dict)
    }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "since": since_label,
        "pricing_mode": "api_direct",
        "prices_fetched_at": prices.get("_last_price_fetch"),
        "prices": prices_export,
        "cursor_api": api_summary if api_summary else None,
        "repos": repos_out,
        "unattributed": {
            **unattributed,
            "estimated_cost_usd": round(unattributed["estimated_cost_usd"], 4),
            "note": "Sessions where no file/workspace context was available",
        },
        "totals": totals,
        "cost_note": cost_note,
    }


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Cursor per-repo activity tracker")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--since",
        metavar="YYYY-MM",
        help="Report from this month onwards (e.g. 2026-01). Always syncs all data.",
    )
    group.add_argument(
        "--last",
        metavar="N",
        type=int,
        help="Report last N days (e.g. --last 30). Always syncs all data.",
    )
    group.add_argument(
        "--from",
        metavar="YYYY-MM-DD",
        dest="from_date",
        help="Report start date inclusive (e.g. 2026-03-01). Combine with --until.",
    )
    parser.add_argument(
        "--until",
        metavar="YYYY-MM-DD",
        help="Report end date inclusive (e.g. 2026-03-31). Combine with --from.",
    )
    parser.add_argument(
        "--output",
        default=str(OUTPUT_FILE),
        help=f"Output file path (default: {OUTPUT_FILE})",
    )
    parser.add_argument(
        "--prices-only",
        action="store_true",
        help="Only fetch+update prices.json from cursor.com, then exit. Outputs JSON result.",
    )
    args = parser.parse_args()

    # ── PRICES-ONLY MODE — fetch rates and exit immediately ───────────────────
    if args.prices_only:
        prices = load_prices()
        old_default = dict(prices.get("default", {}))
        fetched = fetch_cursor_pricing()
        result: dict = {"success": False, "changed": False, "fetched_at": None, "error": None}
        if fetched:
            prices, changed = update_prices_from_cursor(fetched, prices)
            PRICES_FILE.write_text(json.dumps(prices, indent=2), encoding="utf-8")
            ap = fetched.get("auto_pool", {})
            model_count = len(fetched.get("models", {}))
            result = {
                "success": True,
                "changed": changed,
                "fetched_at": prices.get("_last_price_fetch"),
                "auto_pool": ap,
                "model_count": model_count,
                "old_default": old_default,
                "new_default": prices.get("default", {}),
                "error": None,
            }
        else:
            result["error"] = "Could not fetch or parse pricing page (RSC stream empty or 'Auto pricing' section missing)"
        print(json.dumps(result))
        return

    # Determine report date filter (applied to history.db at report time)
    since_ts_ms: int | None = None
    until_ts_ms: int | None = None
    since_label: str = "all-time"

    if args.last:
        cutoff = datetime.now(timezone.utc) - timedelta(days=args.last)
        since_ts_ms = int(cutoff.timestamp() * 1000)
        since_label = f"last-{args.last}-days"
    elif args.since:
        try:
            dt = datetime.strptime(args.since, "%Y-%m").replace(tzinfo=timezone.utc)
            since_ts_ms = int(dt.timestamp() * 1000)
            since_label = args.since
        except ValueError:
            print(f"[error] --since must be in YYYY-MM format, got: {args.since}")
            sys.exit(1)
    elif args.from_date:
        try:
            dt = datetime.strptime(args.from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            since_ts_ms = int(dt.timestamp() * 1000)
            since_label = f"from-{args.from_date}"
        except ValueError:
            print(f"[error] --from must be YYYY-MM-DD, got: {args.from_date}")
            sys.exit(1)

    if args.until:
        try:
            dt_until = datetime.strptime(args.until, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            # Include the full end day (end of day = start of next day - 1 ms)
            until_ts_ms = int((dt_until.timestamp() + 86400 - 0.001) * 1000)
            suffix = f"-until-{args.until}"
            since_label = (since_label + suffix) if since_label != "all-time" else f"until-{args.until}"
        except ValueError:
            print(f"[error] --until must be YYYY-MM-DD, got: {args.until}")
            sys.exit(1)
    # else: all-time (no date filter on report)

    # ── SYNC PHASE — always reads ALL data from Cursor sources ────────────────
    # No date filter on sync so we never miss a conversation in history.db.

    print(f"[tracker] Syncing from Cursor databases ...")

    print(f"[tracker]   Reading state.vscdb bubble tokens ...")
    bubble_tokens = read_bubble_tokens()
    print(f"[tracker]   -> {len(bubble_tokens['by_request'])} requests with real token counts")

    print(f"[tracker]   Reading ai-code-tracking.db ...")
    layer1 = read_ai_tracking(None, bubble_tokens)  # None = no date filter
    known_ids = {c["conversationId"] for c in layer1}
    print(f"[tracker]   -> {len(layer1)} conversations (commit-linked)")

    print(f"[tracker]   Reading workspace SQLite attribution ...")
    workspace_attr = read_workspace_attribution()
    print(f"[tracker]   -> {len(workspace_attr)} conversation-to-workspace mappings")

    print(f"[tracker]   Reading state.vscdb bubbles ...")
    layer2 = read_bubbles(None, known_ids, bubble_tokens, workspace_attr)
    attributed_l2 = sum(1 for c in layer2 if c["repo_path"] != "__unattributed__")
    print(f"[tracker]   -> {len(layer2)} conversations ({attributed_l2} attributed)")

    watcher_entries = read_watcher_log(None)
    if watcher_entries:
        print(f"[tracker]   Watcher log: {len(watcher_entries)} entries")

    prices = load_prices()

    print(f"[tracker]   Fetching pricing from cursor.com ...")
    fetched_pricing = fetch_cursor_pricing()
    if fetched_pricing:
        ap = fetched_pricing.get("auto_pool") or {}
        print(f"[tracker]   -> Auto pool: input=${ap.get('input','?')}/1M  output=${ap.get('output','?')}/1M")
        prices, rates_changed = update_prices_from_cursor(fetched_pricing, prices)
        PRICES_FILE.write_text(json.dumps(prices, indent=2), encoding="utf-8")
        if rates_changed:
            print(f"[tracker]   -> Prices updated in prices.json")
        else:
            print(f"[tracker]   -> No rate changes")
    else:
        print(f"[tracker]   -> Fetch failed — using cached prices.json")

    # Apply watcher fallback before syncing so the layer is recorded
    all_convs = layer1 + layer2
    for conv in all_convs:
        if conv["repo_path"] == "__unattributed__" and watcher_entries:
            ts = conv.get("first_ts")
            if ts:
                repo = watcher_repo_at(ts, watcher_entries)
                if repo:
                    conv["repo_path"] = repo
                    conv["layer"] = "watcher-fallback"

    print(f"[tracker]   Syncing to history.db ...")
    new_c, upd_c = sync_to_history(all_convs, bubble_tokens, prices)
    print(f"[tracker]   -> {new_c} new, {upd_c} updated conversations stored")

    # ── REPORT PHASE — read from history.db with optional date filter ─────────

    print(f"[tracker] Building report (period: {since_label}) ...")

    cookie = load_session_cookie()
    usage_api: dict | None = None
    if cookie:
        month_str = since_label if len(since_label) == 7 else datetime.now().strftime("%Y-%m")
        usage_api = fetch_usage_api(month_str, cookie)

    report = build_report_from_history(since_ts_ms, since_label, prices, usage_api, until_ts_ms)

    out_path = Path(args.output)
    out_path.write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    print(f"[tracker] Written -> {out_path}")

    # Print summary
    print()
    print("-" * 60)
    print(f"  Period      : {since_label}")
    print(f"  Pricing     : per-model rates from prices.json")
    print(f"  Repos tracked: {len(report['repos'])}")
    print()
    for name, r in report["repos"].items():
        print(f"  {name}")
        print(f"    conversations : {r['conversations']}")
        print(f"    AI requests   : {r['requests']}")
        print(f"    files edited  : {r['files_edited']}")
        if r["input_tokens"] > 0:
            out_label = f"{r['output_tokens']:,}" if r["output_tokens"] > 0 else f"~{r['requests'] * avg_output_tokens(prices):,} (flat est.)"
            print(f"    input tokens  : {r['input_tokens']:,}  (actual context window)")
            print(f"    output tokens : {out_label}  (delta est.)")
            print(f"    est. cost     : ${r['estimated_cost_usd']:.4f}")
        else:
            print(f"    est. cost     : n/a (no token data in this period)")
        print(f"    attribution   : {', '.join(r['attribution_layers'])}")
        print()
    if report["unattributed"]["conversations"]:
        u = report["unattributed"]
        print(f"  (unattributed)  conversations: {u['conversations']},  requests: {u['requests']}")
        if u["estimated_cost_usd"] > 0:
            print(f"  (unattributed)  est. cost: ${u['estimated_cost_usd']:.4f}")
    t = report["totals"]
    print()
    print(f"  TOTAL est. cost: ${t['estimated_cost_usd']:.4f}  (per-model API rates)")
    print(f"  NOTE: output tokens estimated from context-window growth between requests.")
    print(f"        Thinking-model sessions may overcount output (thinking tokens inflate delta).")
    print("-" * 60)


if __name__ == "__main__":
    main()
