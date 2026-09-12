"""Partiful RSVP bot — auto-RSVPs to events you describe in plain English.

Quick start
-----------
    pip install playwright && playwright install chromium
    python3 rsvp_bot.py login       # one-time, opens Chrome for SMS auth
    python3 rsvp_bot.py rsvp \\
        --calendar https://www.tech-week.com/calendar/nyc \\
        --types "AI, founder breakfasts, VC drinks"

The bot scrolls the calendar, fetches each event's title + description,
matches them against your `--types`, and RSVPs to the matches.

Matching modes
--------------
  default (free):  keyword/substring match against the types you list
  smart  (LLM):    set ANTHROPIC_API_KEY env var. Bot asks Claude Haiku
                   to judge each event against your description. Catches
                   semantic matches ('drinks party' → wine tasting).
                   Costs ~$0.001 per event.

License
-------
MIT — see LICENSE. Not affiliated with Partiful. Use at your own risk;
Partiful's ToS prohibits automation, you may get account-banned for
high volume.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import random
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout
except ImportError:
    async_playwright = None  # type: ignore

log = logging.getLogger("rsvp_bot")

STATE_FILE = Path("partiful_state.json")
PROFILE_FILE = Path("partiful_profile.json")
LOG_FILE = Path("rsvp_log.csv")

_PARTIFUL_URL_RE = re.compile(r"partiful\.com/e/[A-Za-z0-9]+")
_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_GO_EVENT_RE = re.compile(r"/go/event/[A-Za-z0-9_\-]+")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


# =========================================================================
# login
# =========================================================================

def _auto_detect_profile() -> dict:
    """Best-effort: pull as much profile info as we can from the system
    so the user doesn't have to type anything.

      email     <- `git config user.email`
      name      <- `git config user.name`  (fallback; Partiful localStorage wins)
      bio       <- `gh api user --jq .bio`  (if GitHub CLI is set up)
      linkedin  <- (no reliable auto-source; left blank)
    """
    import subprocess
    out: dict = {}
    def run(cmd: list[str]) -> Optional[str]:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=4)
            v = (r.stdout or "").strip()
            return v or None
        except Exception:
            return None
    out["email"] = run(["git", "config", "--global", "user.email"])
    out["name"] = run(["git", "config", "--global", "user.name"])
    out["bio"] = run(["gh", "api", "user", "--jq", ".bio"])
    out["github_url"] = run(["gh", "api", "user", "--jq", ".html_url"])
    # GitHub user has a `blog` field that's sometimes a LinkedIn URL.
    blog = run(["gh", "api", "user", "--jq", ".blog"])
    if blog and "linkedin.com" in blog:
        out["linkedin"] = blog
    # Filter out empty strings / None
    return {k: v for k, v in out.items() if v}


def _print_manual_login_instructions() -> None:
    print()
    print("=" * 70)
    print("  Partiful login window opened.")
    print("  - Click Sign in / Log in")
    print("  - Enter your phone, type the SMS code")
    print("  - Wait until you see your home feed")
    print("  - Then return here and press ENTER")
    print("=" * 70)


async def _auto_login(page, phone: str) -> bool:
    """Fully automated Partiful login: fill phone → wait for SMS via chat.db → fill OTP.
    Returns True if login succeeded, False if it couldn't be automated."""
    import platform
    # Navigate to Partiful and click the login button.
    try:
        await page.goto("https://partiful.com/", wait_until="networkidle", timeout=30_000)
    except Exception:
        return False

    # Click Sign in / Log in button.
    for sel in ("button:has-text('Log in')", "button:has-text('Sign in')",
                "a:has-text('Log in')", "a:has-text('Sign in')",
                "[role=button]:has-text('Log in')", "[role=button]:has-text('Sign in')"):
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=3_000):
                await loc.click(timeout=3_000)
                break
        except Exception:
            continue
    await page.wait_for_timeout(1_500)

    # Fill in the phone number field.
    phone_filled = False
    for sel in ("input[type='tel']", "input[placeholder*='phone' i]",
                "input[placeholder*='number' i]", "input[inputmode='tel']"):
        try:
            inp = page.locator(sel).first
            if await inp.is_visible(timeout=2_000):
                await inp.fill(phone, timeout=2_000)
                phone_filled = True
                break
        except Exception:
            continue
    if not phone_filled:
        log.debug("auto-login: couldn't find phone field")
        return False

    # Click Send / Continue to request the SMS.
    for sel in ("button:has-text('Send')", "button:has-text('Continue')",
                "button:has-text('Next')", "button:has-text('Get code')",
                "[role=button]:has-text('Send')", "[role=button]:has-text('Continue')"):
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=2_000):
                await loc.click(timeout=2_000)
                break
        except Exception:
            continue
    await page.wait_for_timeout(1_000)

    # Watch chat.db for the OTP (same WAL-watch approach used during RSVP).
    if platform.system() != "Darwin" or _chat_db_path() is None:
        log.debug("auto-login: not on macOS or no chat.db — can't auto-read OTP")
        return False

    print("  Waiting for SMS code via Messages.app...")
    code = await _wait_for_sms_otp_wal(timeout_s=60)
    if not code:
        log.debug("auto-login: no OTP found in chat.db within 60s")
        return False

    print(f"  Got OTP code: {code} — typing it in...")
    # Fill the OTP field.
    otp_filled = False
    for sel in ("input[autocomplete='one-time-code']", "input[inputmode='numeric']",
                "input[type='tel']", "input[name*='code' i]",
                "input[placeholder*='code' i]", "input:visible:not([type='hidden'])"):
        try:
            inp = page.locator(sel).first
            if await inp.is_visible(timeout=2_000):
                try:
                    await inp.fill(code, timeout=2_000)
                except Exception:
                    await inp.click(timeout=1_500)
                    await page.keyboard.type(code, delay=40)
                otp_filled = True
                break
        except Exception:
            continue
    if not otp_filled:
        return False

    # Submit the OTP.
    for sel in ("button:has-text('Verify')", "button:has-text('Continue')",
                "button:has-text('Confirm')", "button:has-text('Submit')",
                "[role=button]:has-text('Verify')", "[role=button]:has-text('Continue')"):
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=2_000):
                await loc.click(timeout=2_000)
                break
        except Exception:
            continue

    # Wait for home feed to confirm login succeeded.
    await page.wait_for_timeout(3_000)
    try:
        body = (await page.inner_text("body")).lower()
        # Partiful home feed has "events" or user-specific content.
        if any(t in body for t in ("upcoming events", "your events", "explore", "discover")):
            return True
    except Exception:
        pass
    # Even if we can't confirm the feed, if OTP was typed we likely succeeded.
    return otp_filled


async def cmd_login(phone: Optional[str] = None, auto: bool = True) -> None:
    """Log into Partiful and save the browser session.

    By default tries fully automated login (phone → SMS via Messages.app → OTP).
    Falls back to manual (headed browser, user types everything) if automation
    fails or --no-auto is passed."""
    if not async_playwright:
        log.error("playwright not installed. pip install playwright && playwright install chromium")
        sys.exit(1)

    # Resolve phone: CLI flag → saved profile → prompt
    if not phone and PROFILE_FILE.exists():
        try:
            saved = json.loads(PROFILE_FILE.read_text())
            phone = saved.get("phone")
        except Exception:
            pass

    import platform
    can_auto = auto and phone and platform.system() == "Darwin" and _chat_db_path() is not None

    async with async_playwright() as p:
        if can_auto:
            print(f"\n  Auto-login: phone={phone}, reading OTP from Messages.app...")
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(viewport={"width": 1200, "height": 900}, user_agent=UA)
            page = await context.new_page()
            success = await _auto_login(page, phone)
            if not success:
                print("  Auto-login failed — falling back to manual mode...")
                await browser.close()
                # Re-launch headed for manual fallback.
                browser = await p.chromium.launch(headless=False)
                context = await browser.new_context(viewport={"width": 1200, "height": 900}, user_agent=UA)
                page = await context.new_page()
                await page.goto("https://partiful.com/", wait_until="networkidle")
                _print_manual_login_instructions()
                input("Press ENTER once logged in... ")
        else:
            if auto and not phone:
                print("  No phone number found — run with --phone +1XXXXXXXXXX for auto-login.")
                print("  Falling back to manual login (headed browser)...")
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context(viewport={"width": 1200, "height": 900}, user_agent=UA)
            page = await context.new_page()
            await page.goto("https://partiful.com/", wait_until="networkidle")
            _print_manual_login_instructions()
            input("Press ENTER once logged in... ")

        await context.storage_state(path=str(STATE_FILE), indexed_db=True)
        print(f"\n✓ saved login state to {STATE_FILE}")

        # Pull what we can from the live Partiful session.
        ext_name, ext_phone = await _extract_user_info(page)
        if not ext_phone and not ext_name:
            print()
            print("  !! No Partiful auth record found — the login did not complete.")
            print("     The saved session is logged out, so every RSVP would skip with")
            print("     'not_logged_in'. Re-run `partiful-rsvp login --no-auto`, and make")
            print("     sure your own event feed is on screen before pressing ENTER.")
        await browser.close()

    # Build the profile from system sources + Partiful. No prompts.
    auto_info = _auto_detect_profile()
    profile = {
        "name": ext_name or auto_info.get("name"),
        "phone": ext_phone or phone,
        "email": auto_info.get("email"),
        "linkedin": auto_info.get("linkedin"),
        "bio": auto_info.get("bio"),
    }
    # Merge over any existing values so reruns don't blow away manual edits.
    if PROFILE_FILE.exists():
        try:
            existing = json.loads(PROFILE_FILE.read_text())
            for k, v in existing.items():
                if v and not profile.get(k):
                    profile[k] = v
        except Exception:
            pass
    PROFILE_FILE.write_text(json.dumps(profile, indent=2))
    print(f"\n✓ auto-detected profile saved to {PROFILE_FILE}:")
    for k, v in profile.items():
        mark = "✓" if v else "·"
        print(f"  {mark} {k:10s} {v or '(none)'}")
    missing = [k for k in ("email", "linkedin", "bio") if not profile.get(k)]
    if missing:
        print()
        print(f"  Missing: {', '.join(missing)}.")
        print(f"  Events with host questionnaires asking for those will be skipped.")
        print(f"  To fill them in, edit {PROFILE_FILE} directly — JSON.")


# =========================================================================
# calendar scrape
# =========================================================================

async def scroll_calendar_cards(calendar_url: str) -> list[dict]:
    """Scroll a Tech Week (or any infinite-scroll) calendar and return one dict
    per event: {"url", "title", "hosts"}.

    Tech Week no longer puts partiful.com links in the calendar HTML. Every
    event is now an opaque /go/event/<token> on tech-week.com that 302s to the
    real host. We keep those URLs as-is rather than pre-resolving them: the
    endpoint hard-blocks bulk access (403/429 within a few dozen requests, and
    plain urllib is refused outright), but a normal browser navigation follows
    the redirect fine. Since the RSVP loop already visits every event with a
    30-60s delay, letting page.goto() follow the 302 costs zero extra requests
    and stays under the block threshold.

    The anchor text carries the title and hosts, so --types can filter without
    fetching anything. Note this filters on title + hosts only, not the event
    description -- pulling descriptions would mean one request per event."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1400, "height": 900},
                                                user_agent=UA)
            page = await context.new_page()
            log.info("scrolling %s", calendar_url)
            await page.goto(calendar_url, wait_until="networkidle", timeout=45_000)
            prev, stable = 0, 0
            for i in range(80):
                n = await page.evaluate(
                    "document.querySelectorAll('table tbody tr:has(a[href*=\"/go/event/\"])')"
                    ".length")
                html = await page.content()
                direct = len(set(_PARTIFUL_URL_RE.findall(html)))
                if i % 5 == 0:
                    log.info("  scroll #%d: %d events (%d redirect, %d direct)",
                             i, n + direct, n, direct)
                if n + direct == prev:
                    stable += 1
                    if stable >= 3:
                        break
                else:
                    stable = 0
                prev = n + direct
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1200)

            # The calendar is a table: TIME | EVENT | HOST | NEIGHBORHOOD.
            # (Anchors outside the table belong to the hero marquee and are
            # duplicates -- each anchor gets its own /go/event/ token, so
            # scraping anchors instead of rows badly overcounts.)
            cards = await page.evaluate(r"""() => {
              const out = [];
              for (const tr of document.querySelectorAll('table tbody tr')) {
                const a = tr.querySelector('a[href*="/go/event/"]');
                if (!a) continue;
                const cells = [...tr.querySelectorAll('td')]
                                .map(td => (td.innerText || '').trim());
                if (cells.length < 2) continue;
                const [time, title, host, hood] = cells;
                if (!title) continue;
                out.push({
                  url: new URL(a.getAttribute('href'), location.origin).href,
                  title: title,
                  hosts: host ? host.split(',').map(h => h.trim()).filter(Boolean) : [],
                  time: time || '',
                  neighborhood: hood || '',
                });
              }
              return out;
            }""")
            # older calendars with plain partiful links still work
            html = await page.content()
            for u in sorted(set(_PARTIFUL_URL_RE.findall(html))):
                cards.append({"url": "https://" + u, "title": "", "hosts": [],
                              "time": "", "neighborhood": ""})
        finally:
            await browser.close()

    seen, uniq = set(), []
    for c in cards:
        if c["url"] in seen:
            continue
        seen.add(c["url"])
        uniq.append(c)
    log.info("calendar done: %d unique events", len(uniq))
    return uniq


async def scroll_calendar(calendar_url: str) -> list[str]:
    """Back-compat wrapper: just the event URLs."""
    return [c["url"] for c in await scroll_calendar_cards(calendar_url)]


# =========================================================================
# event metadata fetch (for filtering)
# =========================================================================

def _fetch_event_meta(event_url: str, timeout: float = 12.0) -> Optional[dict]:
    """Return {title, description, host_orgs} or None on failure."""
    try:
        req = urllib.request.Request(event_url, headers={"User-Agent": UA})
        html = urllib.request.urlopen(req, timeout=timeout).read().decode(errors="replace")
    except Exception:
        return None
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    pp = data.get("props", {}).get("pageProps", {})
    event = pp.get("event") or {}
    hosts = pp.get("hosts") or []
    host_names = [h.get("name") for h in hosts if isinstance(h, dict) and h.get("name")]
    return {
        "url": event_url,
        "title": event.get("title") or "",
        "description": (event.get("description") or "")[:800],
        "hosts": host_names,
    }


# =========================================================================
# matching (keyword OR LLM)
# =========================================================================

def _normalize_types(types: str) -> list[str]:
    """'AI, founder breakfasts, VC drinks' -> ['ai', 'founder breakfasts', 'vc drinks']"""
    return [t.strip().lower() for t in re.split(r"[;,]", types) if t.strip()]


def keyword_match(meta: dict, types: list[str]) -> tuple[bool, str]:
    """Cheap default: any type substring appears in title/description/hosts."""
    blob = " ".join([
        meta.get("title", ""),
        meta.get("description", ""),
        " ".join(meta.get("hosts") or []),
    ]).lower()
    for t in types:
        # split phrase into words; all words must appear (substring) for phrase match
        words = t.split()
        if all(w in blob for w in words):
            return True, t
    return False, ""


def llm_match(meta: dict, user_types: str) -> tuple[bool, str]:
    """Smart mode: Claude Haiku judges whether the event matches the user's
    plain-English description. Skipped if ANTHROPIC_API_KEY isn't set."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return False, "no ANTHROPIC_API_KEY"
    try:
        import anthropic
    except ImportError:
        return False, "anthropic package not installed"
    client = anthropic.Anthropic(api_key=api_key)
    prompt = (
        f"You decide whether an event matches what a user is looking for.\n\n"
        f"User wants: {user_types}\n\n"
        f"Event title: {meta.get('title','')!r}\n"
        f"Event description: {meta.get('description','')[:600]!r}\n"
        f"Event hosts: {', '.join(meta.get('hosts') or [])!r}\n\n"
        f"Reply with exactly one JSON object: "
        f'{{"match": true|false, "reason": "<5 words why>"}}'
    )
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        return False, f"llm error: {type(exc).__name__}"
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    m = re.search(r"\{[^}]+\}", text)
    if not m:
        return False, "llm bad output"
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return False, "llm bad json"
    return bool(data.get("match")), (data.get("reason") or "")[:60]


# =========================================================================
# read name + phone from the logged-in session
# =========================================================================

async def _extract_user_info(page: Page) -> tuple[Optional[str], Optional[str]]:
    """After login, Partiful stores the user's name + phone in browser
    localStorage under Firebase Auth keys. Pull them out so the user
    doesn't have to type --name/--phone for every run."""
    try:
        await page.goto("https://partiful.com/", wait_until="domcontentloaded", timeout=20_000)
    except Exception:
        return (None, None)
    # Wait briefly for client JS to hydrate localStorage.
    await page.wait_for_timeout(1500)
    try:
        data = await page.evaluate("""
          () => {
            const out = {name: null, phone: null};
            for (let i = 0; i < localStorage.length; i++) {
              const k = localStorage.key(i);
              if (!k) continue;
              if (k.startsWith('firebase:authUser:')) {
                try {
                  const v = JSON.parse(localStorage.getItem(k));
                  if (v.phoneNumber) out.phone = v.phoneNumber;
                  if (v.displayName) out.name = v.displayName;
                } catch (e) {}
              }
              if (k.startsWith('CT_user_') || k.includes('partiful')) {
                try {
                  const v = JSON.parse(localStorage.getItem(k));
                  if (v && typeof v === 'object') {
                    if (v.phoneNumber && !out.phone) out.phone = v.phoneNumber;
                    if (v.displayName && !out.name) out.name = v.displayName;
                    if (v.name && !out.name) out.name = v.name;
                  }
                } catch (e) {}
              }
            }
            return out;
          }
        """)
    except Exception:
        return (None, None)
    if isinstance(data, dict) and (data.get("name") or data.get("phone")):
        return (data.get("name"), data.get("phone"))

    # Firebase v9+ keeps the signed-in user in IndexedDB, not localStorage.
    try:
        idb = await page.evaluate(r"""async () => {
          const rows = await new Promise((resolve) => {
            let req;
            try { req = indexedDB.open('firebaseLocalStorageDb'); }
            catch (e) { return resolve(null); }
            req.onerror = () => resolve(null);
            req.onsuccess = () => {
              const db = req.result;
              if (!db.objectStoreNames.contains('firebaseLocalStorage')) return resolve(null);
              const all = db.transaction('firebaseLocalStorage', 'readonly')
                            .objectStore('firebaseLocalStorage').getAll();
              all.onsuccess = () => resolve(all.result);
              all.onerror = () => resolve(null);
            };
            setTimeout(() => resolve(null), 4000);
          });
          const out = {name: null, phone: null};
          for (const r of rows || []) {
            const v = (r && r.value) ? r.value : r;
            if (!v) continue;
            if (v.phoneNumber && !out.phone) out.phone = v.phoneNumber;
            if (v.displayName && !out.name) out.name = v.displayName;
          }
          return out;
        }""")
    except Exception:
        return (None, None)
    if isinstance(idb, dict):
        return (idb.get("name"), idb.get("phone"))
    return (None, None)


# =========================================================================
# rsvp on a single page
# =========================================================================

async def _fill_questionnaire(
    page: Page, *,
    name: Optional[str] = None,
    phone: Optional[str] = None,
    email: Optional[str] = None,
    linkedin: Optional[str] = None,
    bio: Optional[str] = None,
    llm_answer: bool = False,
    debug: bool = False,
) -> str:
    """Find all visible text fields in the second modal, match each to a
    known piece of profile data by label keyword, fall back to LLM if
    enabled. Returns 'filled' on success or a skip reason string."""
    # Find all visible input/textarea elements inside the modal area.
    field_handles = await page.locator(
        "input:visible:not([type='hidden']):not([type='submit']), textarea:visible"
    ).all()
    if not field_handles:
        return "no_fields_found"

    profile = {"name": name, "phone": phone, "email": email, "linkedin": linkedin}
    filled_count = 0
    unfilled_required: list[str] = []

    for inp in field_handles:
        # Skip if already populated.
        try:
            cur = (await inp.input_value()) or ""
            if cur.strip():
                continue
        except Exception:
            pass

        # Get the field's label by scanning DOM neighbours.
        label_text = ""
        try:
            label_text = await inp.evaluate("""(el) => {
              // Try aria-label, placeholder, or the nearest preceding text node.
              let s = el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
              if (s.trim()) return s.trim();
              // Walk up looking for sibling text or a containing block with a label.
              let cur = el.parentElement;
              for (let i = 0; i < 4 && cur; i++) {
                const t = (cur.innerText || '').trim();
                if (t && t.length < 200) return t;
                cur = cur.parentElement;
              }
              return '';
            }""")
        except Exception:
            pass
        label_lower = (label_text or "").lower()

        # Map label keywords to profile fields.
        value: Optional[str] = None
        if "linkedin" in label_lower:
            value = linkedin
        elif "email" in label_lower:
            value = email
        elif "phone" in label_lower or "mobile" in label_lower:
            value = phone
        elif "name" in label_lower:
            value = name
        elif llm_answer:
            value = _llm_answer_question(label_text, profile, bio=bio)

        if not value:
            if "*" in label_text or "required" in label_lower:
                unfilled_required.append(label_text[:60])
            continue

        try:
            await inp.fill(value, timeout=1500)
            filled_count += 1
        except Exception:
            try:
                await inp.click(timeout=1000)
                await page.keyboard.type(value, delay=20)
                filled_count += 1
            except Exception:
                continue

    if unfilled_required:
        return f"requires_questionnaire ({len(unfilled_required)} unanswered: {unfilled_required[0]!r})"
    if filled_count == 0:
        return "no_matched_fields"
    return "filled"


def _llm_answer_question(question: str, profile: dict, bio: Optional[str] = None) -> Optional[str]:
    """Use Claude Haiku to answer one host-questionnaire field."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key or not question:
        return None
    try:
        import anthropic
    except ImportError:
        return None
    client = anthropic.Anthropic(api_key=api_key)
    profile_lines = "\n".join(f"  {k}: {v}" for k, v in profile.items() if v)
    bio_line = f"\nMy bio / one-liner about me: {bio}" if bio else ""
    prompt = (
        f"You're filling out an event RSVP form on someone's behalf.\n\n"
        f"Profile:\n{profile_lines}{bio_line}\n\n"
        f"Question/field: {question!r}\n\n"
        f"Reply with ONLY the answer text (no JSON, no prose, no quotes). "
        f"Keep it under 100 chars. If the question is asking for something "
        f"you don't have, give a brief honest answer."
    )
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:
        return None
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return text.strip().strip('"').strip("'")[:200] or None


_OTP_CODE_RE = re.compile(r"\b(\d{4,8})\b")


def _chat_db_path() -> Optional[Path]:
    import platform
    if platform.system() != "Darwin":
        return None
    db = Path.home() / "Library/Messages/chat.db"
    return db if db.exists() else None


def _read_sms_otp_since_rowid(db: Path, since_rowid: int) -> tuple[Optional[str], int]:
    """Query chat.db for incoming messages with rowid > since_rowid.
    Returns (otp_code_or_None, new_max_rowid).

    Photon's imessage-kit uses the same rowid-bookmark approach — it avoids
    the time-window guessing problem (the `date` column uses Mac Absolute Time
    in nanoseconds, which requires epoch math; rowid is simpler and exact)."""
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
        # Get the highest rowid we've seen so far (for bookmarking).
        (max_rowid,) = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM message").fetchone()
        if max_rowid <= since_rowid:
            return None, since_rowid
        cur = conn.execute(
            "SELECT text FROM message "
            "WHERE rowid > ? AND is_from_me = 0 AND text IS NOT NULL "
            "ORDER BY rowid DESC LIMIT 5",
            (since_rowid,),
        )
        for (text,) in cur.fetchall():
            if not text:
                continue
            for m in _OTP_CODE_RE.finditer(text):
                code = m.group(1)
                if 4 <= len(code) <= 8:
                    return code, max_rowid
        return None, max_rowid
    except sqlite3.OperationalError as exc:
        log.debug("chat.db read failed: %s", exc)
        return None, since_rowid
    except Exception as exc:
        log.debug("chat.db unexpected error: %s", exc)
        return None, since_rowid
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _read_recent_sms_otp(window_seconds: int = 120) -> Optional[str]:
    """Legacy time-window OTP reader. Used as a one-shot check (no bookmark).
    Kept for backwards compatibility with callers that don't hold state."""
    db = _chat_db_path()
    if db is None:
        return None
    import sqlite3
    try:
        import time
        # Mac Absolute Time epoch: 2001-01-01. chat.db stores nanoseconds.
        mac_now_ns = (time.time() - 978_307_200) * 1_000_000_000
        cutoff_ns = mac_now_ns - (window_seconds * 1_000_000_000)
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
        cur = conn.execute(
            "SELECT text FROM message "
            "WHERE date > ? AND is_from_me = 0 AND text IS NOT NULL "
            "ORDER BY date DESC LIMIT 5",
            (cutoff_ns,),
        )
        for (text,) in cur.fetchall():
            if not text:
                continue
            for m in _OTP_CODE_RE.finditer(text):
                code = m.group(1)
                if 4 <= len(code) <= 8:
                    return code
        return None
    except sqlite3.OperationalError as exc:
        log.debug("chat.db read failed: %s", exc)
        return None
    except Exception as exc:
        log.debug("chat.db unexpected error: %s", exc)
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


async def _wait_for_sms_otp_wal(timeout_s: int = 60) -> Optional[str]:
    """WAL-watch OTP reader — the same approach Photon's imessage-kit uses.

    Instead of sleeping N seconds between polls, we watch the SQLite WAL file
    (chat.db-wal) for mtime changes. SQLite writes to the WAL before every
    commit, so a mtime bump = new message committed. We check every 0.5s for
    a WAL change, then query with a rowid bookmark (no epoch math needed).

    This gives ~instant OTP detection (0-0.5s lag) vs the old 3s-sleep loop.
    Falls back to time-window polling if the WAL file doesn't exist (first
    launch, or system just checkpointed and WAL was deleted)."""
    db = _chat_db_path()
    if db is None:
        return None

    wal = Path(str(db) + "-wal")
    import sqlite3

    # Snapshot the current max rowid so we only look at new rows.
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
        (since_rowid,) = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM message").fetchone()
        conn.close()
    except Exception:
        since_rowid = 0

    # Snapshot WAL mtime so we know when it changes.
    try:
        last_wal_mtime = wal.stat().st_mtime if wal.exists() else 0.0
    except Exception:
        last_wal_mtime = 0.0

    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.5)

        # Check if WAL mtime changed (new SQLite commit → new message possible).
        try:
            cur_mtime = wal.stat().st_mtime if wal.exists() else last_wal_mtime
        except Exception:
            cur_mtime = last_wal_mtime

        if cur_mtime != last_wal_mtime:
            last_wal_mtime = cur_mtime
            code, since_rowid = _read_sms_otp_since_rowid(db, since_rowid)
            if code:
                return code
        # Even without a WAL change, poll the DB every 5s as a fallback
        # (covers the case where WAL was checkpointed and deleted).
        elif int(asyncio.get_event_loop().time()) % 5 == 0:
            code, since_rowid = _read_sms_otp_since_rowid(db, since_rowid)
            if code:
                return code

    return None


_SMS_PROMPT_TOKENS = (
    "enter verification code",
    "enter the verification code",
    "enter the code we sent",
    "we sent you a code",
    "we sent a code",
    "verification code",
    "confirm your number",
    "confirm your phone",
    "verify your phone",
    "verify your number",
)


async def _detect_sms_prompt(page: Page) -> bool:
    try:
        body = (await page.inner_text("body")).lower()
    except Exception:
        return False
    return any(t in body for t in _SMS_PROMPT_TOKENS)


async def _wait_for_sms_resolved(page: Page, *, timeout_s: int = 90) -> bool:
    """Poll every 2s for the SMS prompt to disappear. Returns True if it
    resolved (user entered the code), False on timeout."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        if not await _detect_sms_prompt(page):
            return True
        await page.wait_for_timeout(2_000)
    return False


async def _rsvp_one(page: Page, url: str, *, dry_run: bool,
                     name: Optional[str] = None, phone: Optional[str] = None,
                     email: Optional[str] = None, linkedin: Optional[str] = None,
                     bio: Optional[str] = None, llm_answer: bool = False,
                     pause_for_sms: bool = False, auto_sms: bool = False,
                     debug: bool = False, timeout_ms: int = 60_000) -> tuple[str, str]:
    """RSVP to one event. If debug=True, screenshots at each stage and
    keeps the browser open longer so a human can watch."""
    try:
        # A tech-week.com /go/event/<token> URL 302s to the real event here.
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    except PWTimeout:
        return ("navigate", "timeout")
    if "/go/event/" in url:
        if "partiful.com" not in page.url:
            return ("skip", f"not_a_partiful_event | {page.url[:60]}")
        log.info("    -> %s", page.url)
    # React/Next hydration window — without this we look for buttons before
    # the app finishes rendering and almost always miss them.
    try:
        await page.wait_for_load_state("networkidle", timeout=10_000)
    except PWTimeout:
        pass  # some events keep long-poll connections open; carry on
    await page.wait_for_timeout(800)
    title = ""
    try:
        title = (await page.title()) or ""
    except Exception:
        pass
    page_text = (await page.inner_text("body")).lower()
    # Past-event detection — Partiful removes the RSVP button after an
    # event ends. Surface a clearer skip reason than "no button found."
    if any(t in page_text for t in ("event ended", "event has ended",
                                     "this event has passed", "no longer accepting")):
        return ("skip", f"event_ended | {title[:80]}")
    if any(t in page_text for t in ("you're going", "you're attending", "you're in",
                                     "you applied", "you've applied",
                                     "application pending", "you're on the list",
                                     "you're on the waitlist", "you're waitlisted")):
        return ("skip", f"already_rsvpd | {title[:80]}")

    candidates = [
        # invite-only / approval events
        "button:has-text('Apply')",
        "button:has-text('Request to join')",
        "button:has-text('Request invite')",
        "button:has-text('Request to attend')",
        "[role=button]:has-text('Apply')",
        "[role=button]:has-text('Request')",
        # open RSVP
        "button:has-text('Going')",
        "button:has-text('RSVP')",
        "button:has-text(\"I'm going\")",
        "button:has-text('Yes')",
        "button:has-text('Join')",
        "button:has-text('Count me in')",
        "[role=button]:has-text('Going')",
        "[role=button]:has-text('RSVP')",
        # waitlist
        "button:has-text('Join waitlist')",
        "button:has-text('Waitlist')",
    ]
    btn = None
    # Approval-gated events use "Get on the list" rather than an RSVP button.
    # It is written with non-breaking spaces, so CSS text matching misses it,
    # and a plain has-text() would grab the adjacent "Get on the list for full
    # location" teaser instead. Role + exact accessible name gets the real one.
    try:
        loc = page.get_by_role("button", name="Get on the list", exact=True).first
        if await loc.is_visible(timeout=1_500):
            btn = loc
    except Exception:
        pass
    for sel in ([] if btn is not None else candidates):
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1500):
                btn = loc
                break
        except Exception:
            continue
    if btn is None:
        # Distinguish "signed out" from "this event has no RSVP control".
        # Logged-out Partiful shows a Login button where the logged-in nav
        # shows Create. (Do NOT test for "get on the list" here -- logged-in
        # approval-gated events show that too.)
        try:
            signed_out = await page.get_by_role(
                "button", name=re.compile(r"^\s*(log ?in|sign ?in)\s*$", re.I)
            ).first.is_visible(timeout=1_500)
        except Exception:
            signed_out = False
        if signed_out:
            return ("skip", f"not_logged_in | {title[:80]}")
        return ("skip", f"no_rsvp_button_found | {title[:80]}")
    if dry_run:
        try:
            btn_text = (await btn.inner_text(timeout=2000)).strip()
        except Exception:
            btn_text = "?"
        return ("dry-run", f"would_click [{btn_text!r}] | {title[:80]}")
    try:
        await btn.click(timeout=5_000)
    except Exception as exc:
        return ("click", f"click_failed:{type(exc).__name__} | {title[:80]}")

    # Partiful opens a confirm modal with Going/Maybe/Can't Go preselected on
    # Going, plus Name + Phone fields and a Continue button. Fill what we have.
    await page.wait_for_timeout(1200)
    if debug:
        await page.screenshot(path="debug_01_modal_opened.png", full_page=True)

    name_filled, phone_filled = False, False
    if name:
        # Partiful's name field looks like a styled div, not a real <input>.
        # Try the obvious selectors first; if none match, fall back to typing
        # into whichever visible input isn't the phone field.
        attempted = [
            "input[placeholder*='Name' i]",
            "input[placeholder*='your name' i]",
            "input[name='name' i]",
            "input[aria-label*='Name' i]",
            "[contenteditable='true']",
        ]
        for sel in attempted:
            try:
                inp = page.locator(sel).first
                if await inp.is_visible(timeout=1200):
                    try:
                        cur = (await inp.input_value()) or ""
                    except Exception:
                        cur = (await inp.inner_text()) or ""
                    if not cur.strip():
                        try:
                            await inp.fill(name, timeout=1500)
                        except Exception:
                            # contenteditable / styled-div fields ignore fill;
                            # focus + keyboard.type is the reliable fallback.
                            await inp.click(timeout=1500)
                            await page.keyboard.type(name, delay=30)
                    name_filled = True
                    break
            except Exception:
                continue
        # Last-resort fallback: first visible text-ish input that isn't the
        # phone one. Partiful's modal layout is consistent — name is first,
        # phone is second.
        if not name_filled:
            try:
                inputs = page.locator(
                    "input:visible:not([type='tel']):not([inputmode='tel']):not([type='hidden'])"
                )
                first = inputs.first
                if await first.is_visible(timeout=1500):
                    try:
                        await first.fill(name, timeout=1500)
                    except Exception:
                        await first.click(timeout=1500)
                        await page.keyboard.type(name, delay=30)
                    name_filled = True
            except Exception:
                pass
    if phone:
        for sel in ("input[type='tel']",
                    "input[placeholder*='Phone' i]",
                    "input[placeholder*='phone number' i]",
                    "input[name='phone' i]",
                    "input[aria-label*='Phone' i]",
                    "input[inputmode='tel']"):
            try:
                inp = page.locator(sel).first
                if await inp.is_visible(timeout=1500):
                    cur = (await inp.input_value()) or ""
                    if not cur.strip():
                        await inp.fill(phone, timeout=2000)
                    phone_filled = True
                    break
            except Exception:
                continue
    if debug:
        log.info("    debug: name_filled=%s phone_filled=%s", name_filled, phone_filled)
        await page.screenshot(path="debug_02_after_fill.png", full_page=True)

    # Click confirm. 'Continue' is Partiful's modal-submit. Try both <button>
    # and [role=button] / div variants since Partiful uses styled divs.
    clicked_confirm = False
    for sel in ("button:has-text('Continue')",
                "[role=button]:has-text('Continue')",
                "div:has-text('Continue'):not(:has(*))",
                "button:has-text('Confirm')",
                "button:has-text('RSVP')",
                "button:has-text('Submit')",
                "button:has-text('Done')",
                "button:has-text('Save')",
                "button:has-text('Apply')"):
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=2_500):
                await loc.click(timeout=3_000)
                clicked_confirm = True
                break
        except Exception:
            continue
    if debug:
        log.info("    debug: clicked_confirm=%s", clicked_confirm)
    await page.wait_for_timeout(2_500)

    # Partiful sometimes inserts a fresh SMS verification step here even
    # though we're logged in. Try in order:
    #   1. --auto-sms: poll macOS Messages.app for the OTP, type it
    #   2. --pause-for-sms: wait up to 90s for the human to type it
    #   3. neither: skip the event with requires_sms_verification
    if await _detect_sms_prompt(page):
        handled = False
        if auto_sms:
            log.info("    SMS prompt detected — watching Messages.app for OTP code (WAL watch)...")
            code = await _wait_for_sms_otp_wal(timeout_s=60)
            if code:
                log.info("    found OTP code %s — typing into verification field", code)
                # Find the most likely OTP input field and fill it
                typed = False
                for sel in ("input[autocomplete='one-time-code']",
                            "input[inputmode='numeric']",
                            "input[type='tel']",
                            "input[name*='code' i]",
                            "input[placeholder*='code' i]",
                            "input:visible:not([type='hidden'])"):
                    try:
                        inp = page.locator(sel).first
                        if await inp.is_visible(timeout=1500):
                            try:
                                await inp.fill(code, timeout=2000)
                            except Exception:
                                await inp.click(timeout=1500)
                                await page.keyboard.type(code, delay=40)
                            typed = True
                            break
                    except Exception:
                        continue
                if typed:
                    # Find a submit/continue button and click
                    for sel in ("button:has-text('Verify')",
                                "button:has-text('Continue')",
                                "button:has-text('Confirm')",
                                "button:has-text('Submit')"):
                        try:
                            loc = page.locator(sel).first
                            if await loc.is_visible(timeout=1500):
                                await loc.click(timeout=2000)
                                break
                        except Exception:
                            continue
                    await page.wait_for_timeout(2500)
                    if not await _detect_sms_prompt(page):
                        handled = True
                        log.info("    SMS verified, continuing")
        if not handled and pause_for_sms:
            log.info("    SMS verification prompt — pausing 90s for you to type "
                     "the code in the browser. Waiting...")
            ok = await _wait_for_sms_resolved(page, timeout_s=90)
            if not ok:
                return ("skip", f"sms_verification_timeout | {title[:80]}")
            handled = True
            log.info("    SMS resolved, continuing")
        if not handled:
            return ("skip", f"requires_sms_verification | {title[:80]}")

    # Some events have a SECOND modal — host-defined questionnaire
    # ("Questions from the hosts") with custom fields. Handle up to 3
    # sequential modals so multi-step funnels work.
    questionnaire_handled = False
    questionnaire_skip_reason = None
    for round_idx in range(3):
        try:
            body = (await page.inner_text("body")).lower()
        except Exception:
            break
        # Heuristic for "we hit another modal that wants more answers"
        is_questionnaire = ("questions from the hosts" in body
                            or "tell us about yourself" in body
                            or "answer a few questions" in body)
        if not is_questionnaire:
            break
        result = await _fill_questionnaire(
            page, name=name, phone=phone, email=email, linkedin=linkedin,
            bio=bio, llm_answer=llm_answer, debug=debug,
        )
        if result == "filled":
            questionnaire_handled = True
            # Click Continue again to submit
            for sel in ("button:has-text('Continue')",
                        "[role=button]:has-text('Continue')"):
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=2_500):
                        await loc.click(timeout=3_000)
                        break
                except Exception:
                    continue
            await page.wait_for_timeout(2_500)
        else:
            questionnaire_skip_reason = result
            break
    if debug and questionnaire_handled:
        await page.screenshot(path="debug_04_after_questionnaire.png", full_page=True)

    if debug:
        await page.screenshot(path="debug_03_after_confirm.png", full_page=True)
        log.info("    debug: holding browser open for 20s for inspection")
        await page.wait_for_timeout(20_000)

    if questionnaire_skip_reason:
        return ("skip", f"{questionnaire_skip_reason} | {title[:80]}")
    try:
        post_text = (await page.inner_text("body")).lower()
        if any(t in post_text for t in ("you're going", "you're attending", "you're in",
                                        "you applied", "you've applied",
                                        "pending approval", "request submitted", "request sent",
                                        "you're on the list", "you're on the waitlist")):
            return ("rsvp", f"success | {title[:80]}")
    except Exception:
        pass
    return ("rsvp", f"unknown_state | {title[:80]}")


# =========================================================================
# top-level rsvp command
# =========================================================================

async def cmd_rsvp(
    *,
    calendar_url: Optional[str] = None,
    urls_file: Optional[Path] = None,
    types: Optional[str] = None,
    use_llm: bool = False,
    delay: float = 30.0,
    jitter: float = 30.0,
    max_events: Optional[int] = None,
    dry_run: bool = False,
    headed: bool = False,
    name: Optional[str] = None,
    phone: Optional[str] = None,
    email: Optional[str] = None,
    linkedin: Optional[str] = None,
    bio: Optional[str] = None,
    llm_answer: bool = False,
    pause_for_sms: bool = False,
    auto_sms: bool = False,
    debug: bool = False,
) -> None:
    if not async_playwright:
        log.error("playwright not installed")
        sys.exit(1)
    if not STATE_FILE.exists():
        log.error(f"no login state at {STATE_FILE} — run `python3 rsvp_bot.py login` first")
        sys.exit(1)

    # Source URLs. Calendar scraping also yields title/hosts per event, which
    # lets --types filter without a request per event.
    cards: Optional[list[dict]] = None
    if calendar_url:
        cards = await scroll_calendar_cards(calendar_url)
        urls = [c["url"] for c in cards]
    elif urls_file:
        urls = [u.strip() for u in urls_file.read_text().splitlines()
                if u.strip() and not u.startswith("#")]
    else:
        log.error("need --calendar URL or --urls FILE")
        sys.exit(1)

    # Optional filter
    if types:
        type_list = _normalize_types(types)
        log.info("filtering %d events against types: %s", len(urls), type_list)
        kept: list[tuple[str, str]] = []  # (url, why_matched)
        by_url = {c["url"]: c for c in (cards or [])}
        for i, u in enumerate(urls, 1):
            if i % 25 == 0:
                log.info("  filter %d/%d — keeping %d so far", i, len(urls), len(kept))
            card = by_url.get(u)
            if card and card.get("title"):
                # Title + hosts from the calendar itself; no request needed.
                # No description without a request per event; the
                # neighbourhood is included so --types can match on location.
                meta = {"url": u, "title": card["title"],
                        "description": card.get("neighborhood", ""),
                        "hosts": card["hosts"]}
            else:
                meta = _fetch_event_meta(u)
            if not meta:
                continue
            if use_llm:
                hit, why = llm_match(meta, types)
            else:
                hit, why = keyword_match(meta, type_list)
            if hit:
                kept.append((u, why))
        log.info("filter kept %d / %d events", len(kept), len(urls))
        urls = [u for u, _ in kept]
        # Persist filter result so re-runs are cheap
        Path("rsvp_filtered.csv").open("w", newline="").write(
            "url,reason\n" + "\n".join(f"{u},{w}" for u, w in kept))
        log.info("saved filtered set to rsvp_filtered.csv")

    if max_events is not None:
        urls = urls[:max_events]
    log.info("queueing %d events (delay=%.0fs±%.0f, dry_run=%s)",
             len(urls), delay, jitter, dry_run)

    log_existed = LOG_FILE.exists()
    f = LOG_FILE.open("a", newline="", encoding="utf-8")
    w = csv.writer(f)
    if not log_existed:
        w.writerow(["timestamp", "url", "action", "result"])
    f.flush()

    # Resolve name + phone in priority order:
    #   1. --name/--phone CLI flags
    #   2. saved partiful_profile.json from a previous run
    #   3. auto-extract from Firebase Auth in localStorage
    # Whatever we end up with gets written back to the profile so the next
    # run picks it up without any flags.
    if PROFILE_FILE.exists():
        try:
            saved = json.loads(PROFILE_FILE.read_text())
            for k, v in (("name", name), ("phone", phone), ("email", email),
                         ("linkedin", linkedin), ("bio", bio)):
                if not v and saved.get(k):
                    log.info("loaded %s from %s", k, PROFILE_FILE)
                    if k == "name": name = saved[k]
                    elif k == "phone": phone = saved[k]
                    elif k == "email": email = saved[k]
                    elif k == "linkedin": linkedin = saved[k]
                    elif k == "bio": bio = saved[k]
        except Exception:
            pass

    consecutive_fail = 0
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not headed)
        context = await browser.new_context(storage_state=str(STATE_FILE))
        page = await context.new_page()
        # Last-resort extraction from the live session.
        if not name or not phone:
            ext_name, ext_phone = await _extract_user_info(page)
            if not name and ext_name:
                name = ext_name
                log.info("auto-extracted name from login: %s", name)
            if not phone and ext_phone:
                phone = ext_phone
                log.info("auto-extracted phone from login: %s", phone)
        # Persist whatever we ended up with so the next run is zero-config.
        if name or phone or email or linkedin or bio:
            try:
                PROFILE_FILE.write_text(json.dumps({
                    "name": name, "phone": phone, "email": email,
                    "linkedin": linkedin, "bio": bio,
                }))
            except Exception:
                pass
        if not phone:
            log.warning("no phone resolved — Partiful's modal may not accept the RSVP. "
                        "Pass --phone explicitly once and it'll be saved for next time.")
        for i, url in enumerate(urls, 1):
            log.info("[%d/%d] %s", i, len(urls), url)
            action, result = await _rsvp_one(page, url, dry_run=dry_run,
                                              name=name, phone=phone,
                                              email=email, linkedin=linkedin,
                                              bio=bio, llm_answer=llm_answer,
                                              pause_for_sms=pause_for_sms,
                                              auto_sms=auto_sms,
                                              debug=debug)
            w.writerow([datetime.now(timezone.utc).isoformat(), url, action, result])
            f.flush()
            log.info("    %s | %s", action, result)
            if action in ("click", "navigate") and "fail" in result.lower():
                consecutive_fail += 1
            else:
                consecutive_fail = 0
            if consecutive_fail >= 3:
                log.error("3 consecutive failures — bailing. account may be flagged.")
                break
            if i < len(urls):
                wait = delay + random.uniform(0, jitter)
                log.info("    sleeping %.1fs", wait)
                await asyncio.sleep(wait)
        try:
            # indexed_db=True keeps the Firebase auth record alive between runs.
            await context.storage_state(path=str(STATE_FILE), indexed_db=True)
        except Exception:
            pass
        await browser.close()
    f.close()
    log.info("done. log at %s", LOG_FILE)


# =========================================================================
# cli
# =========================================================================

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    lp = sub.add_parser("login", help="one-time login; auto-reads SMS OTP from Messages.app")
    lp.add_argument("--phone", type=str, default=None,
                    help="your phone number (e.g. +1XXXXXXXXXX). Required for auto-login. "
                         "Saved to partiful_profile.json so you only need to pass it once.")
    lp.add_argument("--no-auto", action="store_true",
                    help="skip auto-login and open a headed browser for manual login instead.")

    rp = sub.add_parser("rsvp", help="run RSVPs")
    src = rp.add_mutually_exclusive_group(required=True)
    src.add_argument("--calendar", type=str,
                     help="Tech-week-style calendar URL (e.g. https://www.tech-week.com/calendar/nyc)")
    src.add_argument("--urls", type=Path, help="text file with one Partiful URL per line")
    rp.add_argument("--types", type=str, default=None,
                    help='comma-separated event types you want, e.g. "AI, founder breakfasts, VC drinks". '
                         'When set, the bot fetches each event and only RSVPs to matching ones.')
    rp.add_argument("--llm", action="store_true",
                    help="use Claude Haiku to judge matches semantically. "
                         "Requires ANTHROPIC_API_KEY env var (~$0.001 per event).")
    rp.add_argument("--delay", type=float, default=30.0, help="base seconds between RSVPs")
    rp.add_argument("--jitter", type=float, default=30.0, help="random extra seconds")
    rp.add_argument("--max", type=int, default=None, help="optional cap on events this run")
    rp.add_argument("--name", type=str, default=None,
                    help="name to fill in Partiful's RSVP modal if it asks. "
                         "Use the same name on your Partiful account.")
    rp.add_argument("--phone", type=str, default=None,
                    help="phone number for the RSVP modal (e.g. '+1 555 123 4567'). "
                         "Partiful uses this for event reminders.")
    rp.add_argument("--email", type=str, default=None,
                    help="email for host questionnaires that ask for one.")
    rp.add_argument("--linkedin", type=str, default=None,
                    help="LinkedIn URL for host questionnaires that ask for one "
                         "(e.g. https://linkedin.com/in/danielwang).")
    rp.add_argument("--bio", type=str, default=None,
                    help="one-line bio used by --llm-answer to fill custom questions "
                         "(e.g. 'CS at UWaterloo, building a partiful rsvp bot').")
    rp.add_argument("--llm-answer", action="store_true",
                    help="for host-questionnaire fields the bot doesn't recognize, "
                         "use Claude Haiku to compose answers from your profile + bio. "
                         "Requires ANTHROPIC_API_KEY. Without this, events with custom "
                         "questions are skipped.")
    rp.add_argument("--pause-for-sms", action="store_true",
                    help="if Partiful asks for an SMS verification code mid-RSVP, "
                         "pause up to 90s for you to type it into the browser (use "
                         "with --headed). Without this flag, events that require "
                         "SMS verification are skipped.")
    rp.add_argument("--auto-sms", action="store_true",
                    help="(macOS only) when Partiful prompts for an SMS code, read "
                         "the most recent Messages.app text via ~/Library/Messages/chat.db "
                         "and type the OTP automatically. Requires Full Disk Access "
                         "granted to your Terminal app. Falls back to --pause-for-sms "
                         "if the read fails (no permission, empty inbox, etc).")
    rp.add_argument("--dry-run", action="store_true", help="navigate + report, don't click")
    rp.add_argument("--headed", action="store_true", help="visible browser (debugging)")
    rp.add_argument("--debug", action="store_true",
                    help="save screenshots (debug_01..03_*.png), log every modal step, "
                         "and hold the browser open 20s after Continue so you can inspect "
                         "what landed. Use to diagnose unknown_state results.")
    rp.add_argument("--quiet", action="store_true")

    args = ap.parse_args()
    logging.basicConfig(
        level=logging.WARNING if getattr(args, "quiet", False) else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.cmd == "login":
        asyncio.run(cmd_login(phone=args.phone, auto=not args.no_auto))
    elif args.cmd == "rsvp":
        asyncio.run(cmd_rsvp(
            calendar_url=args.calendar,
            urls_file=args.urls,
            types=args.types,
            use_llm=args.llm,
            delay=args.delay,
            jitter=args.jitter,
            max_events=args.max,
            dry_run=args.dry_run,
            headed=args.headed,
            name=args.name,
            phone=args.phone,
            email=args.email,
            linkedin=args.linkedin,
            bio=args.bio,
            llm_answer=args.llm_answer,
            pause_for_sms=args.pause_for_sms,
            auto_sms=args.auto_sms,
            debug=args.debug,
        ))


if __name__ == "__main__":
    main()
