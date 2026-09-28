#!/usr/bin/env python3
"""
Bevakar lediga tider på Timecenter (Hyllie Idrottsskadeklinik) och skickar
push-notis via ntfy.sh när NYA lediga tider inom N dagar dyker upp.

Användning:
  python bevaka_tider.py              # en körning (för GitHub Actions / cron)
  python bevaka_tider.py --loop 15    # kör var 15:e minut på egen dator
  python bevaka_tider.py --debug      # sparar HTML + skärmdump i debug/ och visar vad som tolkas
  python bevaka_tider.py --test-notis # skickar en testnotis
"""
import argparse
import asyncio
import datetime as dt
import json
import os
import random
import re
import sys
import time
import urllib.request
from pathlib import Path

URL = os.environ.get(
    "BOKNINGS_URL",
    "https://m.timecenter.se/hyllieidrottsskadeklinik/boka/time/?rid=46069&tjid=127195&tidval=45",
)
DAGAR = int(os.environ.get("DAGAR_FRAMAT", "14"))
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
STATE = Path(os.environ.get("STATE_FILE", "sedda_tider.json"))
DEBUG_DIR = Path("debug")
MAX_SIDOR = 4  # hur många "nästa vecka"-klick som max görs

MANADER = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "maj": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dec": 12}
VECKODAGAR = ["mån", "tis", "ons", "tor", "fre", "lör", "sön"]
MANADSNAMN = ["jan", "feb", "mar", "apr", "maj", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]

# Går igenom sidan i dokumentordning, håller reda på senast sedda datumrubrik
# och plockar ut klickbara element vars text är en klockslag (t.ex. 08:30).
EXTRACT_JS = r"""
() => {
  const timeRe = /^\s*(\d{1,2})[:.](\d{2})(?:\s*[-–]\s*\d{1,2}[:.]\d{2})?\s*$/;
  const dateRe = /(\d{4}-\d{2}-\d{2})|(\b\d{1,2}\s*[\/.]\s*\d{1,2}\b)|(\b\d{1,2}\s+(jan|feb|mar|apr|maj|jun|jul|aug|sep|okt|nov|dec))|\b(idag|i dag|imorgon|i morgon)\b/i;
  const badRe = /disabled|bokad|upptagen|full|occupied|booked|inactive|unavailable|passed/i;
  const cls = e => (e && e.getAttribute) ? ((e.getAttribute('class') || '') + ' ' + (e.getAttribute('title') || '')) : '';
  const tableDate = el => {
    const td = el && el.closest && el.closest('td,th'); if (!td) return null;
    const table = td.closest('table'); if (!table) return null;
    const head = table.tHead ? table.tHead.rows[0] : table.rows[0];
    if (!head || head === td.parentElement) return null;
    const h = head.cells[td.cellIndex];
    const t = h ? h.innerText.trim() : '';
    return dateRe.test(t) ? t : null;
  };
  const out = []; let cur = null;
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
  let n;
  while ((n = w.nextNode())) {
    let text, el;
    if (n.nodeType === 3) { el = n.parentElement; text = n.textContent; }
    else if (n.tagName === 'INPUT' && n.value) { el = n; text = n.value; }
    else continue;
    if (!el || el.closest('script,style,noscript')) continue;
    text = (text || '').trim(); if (!text) continue;
    const tm = text.match(timeRe);
    if (tm) {
      const clickEl = el.closest('a,button,label,option,[onclick],[role=button]') || (el.tagName === 'INPUT' ? el : null);
      const inactive = !!el.closest('[disabled],[aria-disabled=true]') ||
                       [el, clickEl, el.parentElement].some(e => badRe.test(cls(e)));
      out.push({
        date: tableDate(clickEl || el) || cur,
        time: tm[1].padStart(2, '0') + ':' + tm[2],
        clickable: !!clickEl,
        inactive,
        href: clickEl ? ((clickEl.getAttribute('href') || '') + ' ' + (clickEl.getAttribute('onclick') || '') + ' ' + (clickEl.getAttribute('value') || '')) : ''
      });
      continue;
    }
    if (text.length < 60 && dateRe.test(text)) cur = text;
  }
  return out;
}
"""

# Hittar en "nästa vecka"/"framåt"-knapp och märker den så att Playwright kan klicka.
NEXT_JS = r"""
() => {
  const re = /^(nästa|nasta|next|framåt|senare|visa fler|fler tider|›|»|>|→|>>)/i;
  const els = [...document.querySelectorAll('a,button,[role=button],input[type=button],input[type=submit]')];
  for (const e of els) {
    const t = (e.innerText || e.value || e.getAttribute('aria-label') || e.title || '').trim();
    if (re.test(t) && !e.disabled) { e.setAttribute('data-bevakning-nasta', '1'); return t; }
  }
  return null;
}
"""


def _datum(y, m, d):
    try:
        return dt.date(y, m, d)
    except ValueError:
        return None


def _gissa_ar(m, d, idag):
    kand = _datum(idag.year, m, d)
    if kand and kand < idag - dt.timedelta(days=30):
        kand = _datum(idag.year + 1, m, d)
    return kand


def tolka_datum(s, idag):
    """Försöker tolka ett datum ur en text eller en länk. Returnerar date eller None."""
    if not s:
        return None
    s = s.strip()
    low = s.lower()
    if m := re.search(r"(20\d{2})-(\d{2})-(\d{2})", s):
        return _datum(int(m[1]), int(m[2]), int(m[3]))
    if m := re.search(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)", s):
        return _datum(int(m[1]), int(m[2]), int(m[3]))
    if re.search(r"\bi ?dag\b", low):
        return idag
    if re.search(r"\bi ?morgon\b", low):
        return idag + dt.timedelta(days=1)
    if m := re.search(r"(?<!\d)(\d{1,2})\s+(jan|feb|mar|apr|maj|jun|jul|aug|sep|okt|nov|dec)[a-zåäö]*\.?(?:\s+(20\d{2}))?", low):
        d, mo = int(m[1]), MANADER[m[2]]
        return _datum(int(m[3]), mo, d) if m[3] else _gissa_ar(mo, d, idag)
    if m := re.search(r"(?<![\d:])(\d{1,2})\s*[/.]\s*(\d{1,2})(?:\s*[/.]\s*(\d{2,4}))?(?![\d:])", s):
        d, mo = int(m[1]), int(m[2])
        if m[3]:
            y = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
            return _datum(y, mo, d)
        return _gissa_ar(mo, d, idag)
    return None


def formatera(datum_iso, tid):
    if datum_iso == "okänt datum":
        return f"kl {tid} (datum kunde inte tolkas)"
    d = dt.date.fromisoformat(datum_iso)
    return f"{VECKODAGAR[d.weekday()]} {d.day} {MANADSNAMN[d.month - 1]} kl {tid}"


async def hamta_tider(debug=False):
    from playwright.async_api import async_playwright

    idag = dt.date.today()
    slut = idag + dt.timedelta(days=DAGAR)
    hittade = set()
    if debug:
        DEBUG_DIR.mkdir(exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(locale="sv-SE", timezone_id="Europe/Stockholm",
                                      viewport={"width": 420, "height": 900})
        await page.goto(URL, wait_until="networkidle", timeout=60000)

        for sida in range(MAX_SIDOR):
            if debug:
                (DEBUG_DIR / f"sida{sida}.html").write_text(await page.content(), encoding="utf-8")
                await page.screenshot(path=str(DEBUG_DIR / f"sida{sida}.png"), full_page=True)
                print(f"\n=== Sida {sida}: {page.url}")

            rader = await page.evaluate(EXTRACT_JS)
            senaste = None
            odaterade = []
            for r in rader:
                d = tolka_datum(r["href"], idag) or tolka_datum(r["date"], idag)
                if debug:
                    print(f"  tid={r['time']} datumtext={r['date']!r} -> {d} "
                          f"klickbar={r['clickable']} inaktiv={r['inactive']}")
                if not r["clickable"] or r["inactive"]:
                    continue
                if d is None:
                    odaterade.append(r["time"])
                    continue
                senaste = max(senaste or d, d)
                if idag <= d <= slut:
                    hittade.add((d.isoformat(), r["time"]))
            if odaterade and senaste is None:
                hittade.update(("okänt datum", t) for t in odaterade)

            if senaste and senaste > slut:
                break
            fore = await page.evaluate("document.body.innerText")
            knapp = await page.evaluate(NEXT_JS)
            if not knapp:
                if debug:
                    print("  (ingen 'nästa'-knapp hittades)")
                break
            if debug:
                print(f"  klickar på '{knapp}'")
            await page.click("[data-bevakning-nasta]")
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            await page.wait_for_timeout(1500)
            if await page.evaluate("document.body.innerText") == fore:
                break
        await browser.close()
    return sorted(hittade)


def skicka_notis(titel, text):
    if not NTFY_TOPIC:
        print("NTFY_TOPIC saknas – ingen notis skickad. Meddelande:\n" + text)
        return
    req = urllib.request.Request(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=text.encode("utf-8"),
        method="POST",
        headers={"Title": titel, "Click": URL, "Tags": "calendar", "Priority": "high"},
    )
    urllib.request.urlopen(req, timeout=20).read()


def las_state():
    try:
        return {tuple(x) for x in json.loads(STATE.read_text(encoding="utf-8"))}
    except Exception:
        return set()


def kor_en_gang(debug=False):
    tider = asyncio.run(hamta_tider(debug))
    nu = set(tider)
    nya = sorted(nu - las_state())
    print(f"{dt.datetime.now():%Y-%m-%d %H:%M} – {len(nu)} lediga tider inom {DAGAR} dagar, {len(nya)} nya")
    if nya:
        rader = [formatera(d, t) for d, t in nya[:12]]
        if len(nya) > 12:
            rader.append(f"... och {len(nya) - 12} till")
        skicka_notis("Lediga tider - Hyllie Idrottsskadeklinik", "\n".join(rader) + "\n\nTryck for att boka.")
    # Spara aktuellt läge: tider som försvinner och sedan dyker upp igen (avbokningar) notifieras på nytt.
    STATE.write_text(json.dumps(sorted(nu), ensure_ascii=False), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--test-notis", action="store_true")
    ap.add_argument("--loop", type=float, metavar="MINUTER", help="kör om och om igen med detta intervall")
    a = ap.parse_args()

    if a.test_notis:
        skicka_notis("Test - tidsbevakning", "Om du ser detta fungerar notiserna.")
        print("Testnotis skickad.")
        return

    if not a.loop:
        try:
            kor_en_gang(a.debug)
        except Exception as e:
            print(f"Fel vid hämtning: {e}", file=sys.stderr)
            sys.exit(1)
        return

    while True:
        try:
            kor_en_gang(a.debug)
        except Exception as e:
            print(f"Fel vid hämtning (försöker igen senare): {e}", file=sys.stderr)
        time.sleep(a.loop * 60 + random.uniform(0, 60))


if __name__ == "__main__":
    main()
