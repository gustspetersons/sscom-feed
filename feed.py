"""RSS feed of new ss.com flat ads in Rīga's Centrs and Vecrīga.

An ad reaches the feed when it has at least `min_rooms` rooms and either

- rents by the month for a total, utilities included, within `rent.max_total`.
  Unless the ad says its rent covers utilities, they are estimated from the
  flat's area: `rent.utilities_base` + `rent.utilities_per_m2` × m²; or
- sells for at most `sale.max_price`.

The settings live in config.toml. An ad counts as new the first time its ID
shows up in the listings, which ss.com sorts newest first. An ad lifted back to
the top keeps its ID, so it never comes back. A flat posted again under a new
ID (same deal, street, rooms, area and floor) stays out as well, unless its
price is now lower than when it was in the feed.

State lives next to the feed in the site directory (the gh-pages branch):

    seen.csv    every ad ID ever seen, with the time it was first seen
    state.json  the listing sections already read in full once
    items.json  the ads behind the feed's items; each stays REPOST_DAYS, to
                catch its flat posted again
    feed.xml    the RSS feed itself
"""

import argparse
import csv
import html
import http.client
import json
import re
import time
import tomllib
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import NamedTuple
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

SITE = "https://www.ss.com"
DISTRICT_URL = SITE + "/lv/real-estate/flats/riga/{district}/"
SECTION_URL = DISTRICT_URL + "{deal}/"
FEED_URL = "https://gustspetersons.github.io/sscom-feed/feed.xml"
USER_AGENT = "sscom-feed/1.0 (personal RSS; github.com/gustspetersons/sscom-feed)"
CONFIG_FILE = Path("config.toml")

DEALS = {"hand_over": "Izīrē", "sell": "Pārdod"}
# ss.com's names for districts in its URLs: (name, locative for the title)
DISTRICTS = {"centre": ("Centrs", "Centrā"), "vecriga": ("Vecrīga", "Vecrīgā")}

MAX_PAGES = 10  # listing pages per section on an ordinary run
MAX_SCAN_PAGES = 150  # listing pages per section when reading it in full
REQUEST_GAP = 1.5  # seconds between requests, to go easy on ss.com
ATTEMPTS = 4  # tries per request before the run gives up
RETRY_WAIT = 15  # seconds before the first retry, doubled for each one after
MAX_QUIET_DAYS = 2  # days without a single new ad before it's a problem
FEED_DAYS = 14  # how long an ad stays in the feed
REPOST_DAYS = 60  # how long an ad is kept to catch its flat posted again
SNIPPET = 400  # characters of the ad's text shown in its item
# Problems that should fail the run once state is saved; the workflow's
# "Check feed health" step turns each line into an error, which GitHub emails.
PROBLEMS_FILE = Path("problems.txt")
RIGA = ZoneInfo("Europe/Riga")

# The listing's data columns, keyed by header text without spaces, commas and
# dots. Rows are matched to them from the right, so the checkbox, photo and
# title cells on the left can't shift them.
COLUMNS = {
    "iela": "street",
    "ist": "rooms",
    "m2": "area",
    "m²": "area",
    "stāvs": "floor",
    "sērija": "series",
    "cenam2": "price_m2",
    "cenam²": "price_m2",
    "cena": "price",
}
DEFAULT_COLUMNS = ["street", "rooms", "area", "floor", "series", "price_m2", "price"]
AD_ROW = re.compile(r"tr_(\d+)$")
DATE = re.compile(r"Datums:\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\.?\s+(\d{1,2}):(\d{2})")
TEXT_START = ("Aizvērt karti", "Atgriezties uz sludinājumu sarakstu")

# What an ad's text says about utilities, matched in lower case (Latvian,
# English, Russian). "Included" counts only when nothing says "extra". Loose
# phrases such as "ar komunālajiem" stay out of both lists: as often as not
# they give the total with utilities on top ("ar komunālajiem ~650 EUR").
UTILITIES_INCLUDED = [
    re.compile(pattern)
    for pattern in [
        r"komunāl\w*(?: maksājum\w*| pakalpojum\w*)? (?:ir |jau )?"
        r"(?:iekļaut|ieskaitīt)",
        r"\b(?:ieskaitot|iekļaujot) (?:visus )?komunāl",
        r"\b(?:iekļaut|ieskaitīt)\w* (?:arī |visi )?komunāl",
        r"\bviss iekļauts|\bvisi maksājumi (?:ir )?iekļauti",
        r"\b(?:utilities|bills)(?: are)? included",
        r"\b(?:including|incl\.?)(?: all)? (?:utilities|bills)",
        r"\ball[ -]inclusive",
        r"коммунальн\w*(?: платеж\w*| услуг\w*)? (?:включен|входят)",
        r"\bвключая коммунальн",
        r"\bвс[её] включено",
    ]
]
UTILITIES_EXTRA = [
    re.compile(pattern)
    for pattern in [
        r"(?:\bbez|\bneieskaitot|\bneiekļaujot|\bneskaitot|\bizņemot|\bplus|\+)"
        r" ?komunāl",
        r"komunāl\w*(?: maksājum\w*| pakalpojum\w*)? (?:nav|netiek) "
        r"(?:iekļaut|ieskaitīt)",
        r"\b(?:nav|netiek) (?:iekļaut|ieskaitīt)\w* (?:arī |visi )?komunāl",
        r"komunāl\w*(?: maksājum\w*)? (?:atsevišķi|papildus|pēc skaitītāj)",
        r"komunāl\w*(?: maksājum\w*)? (?:maksā|apmaksā|sedz) īrniek",
        r"\bīrniek\w* (?:pats |pati )?(?:maksā|apmaksā|sedz) (?:arī )?komunāl",
        r"(?:\bexcluding|\bexcl\.?|\bwithout|\bplus|\+) ?(?:utilities|bills)",
        r"\b(?:utilities|bills)(?: are)? (?:not included|extra|separate)",
        r"\b(?:does not|doesn't|not) includ\w* (?:the )?(?:utilities|bills)",
        r"\btenant pays (?:the |all )?(?:utilities|bills)",
        r"(?:\bбез(?: уч[её]та)?|\bне включая|\bплюс|\+) ?коммунальн",
        r"коммунальн\w*(?: платеж\w*| услуг\w*)? (?:не включен|отдельно|оплачива)",
    ]
]
UTILITY_WORDS = re.compile(r"komunāl|utilit|\bbills\b|коммунал", re.IGNORECASE)
SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[A-ZĀČĒĢĪĶĻŅŠŪŽА-ЯЁ„\"(])|\n+")


class Section(NamedTuple):
    district: str  # as in ss.com's URLs: centre, vecriga
    deal: str  # hand_over (Izīrē) or sell (Pārdod)

    @property
    def key(self):
        return f"{self.district}/{self.deal}"

    @property
    def url(self):
        return SECTION_URL.format(district=self.district, deal=self.deal)


def squash(text):
    """Collapse runs of whitespace, no-break spaces included, to one space."""
    return " ".join((text or "").split())


def fold(text):
    """Normalise text for matching: Unicode NFC, lower case, single spaces."""
    return squash(unicodedata.normalize("NFC", text or "").casefold())


def clean_text(text):
    """Squash each line and drop the empty ones, keeping the line breaks."""
    lines = (squash(line) for line in (text or "").splitlines())
    return "\n".join(line for line in lines if line)


def shorten(text, limit):
    """Cut text to `limit` characters at a word break, marked with "…"."""
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:") + " …"


def parse_time(value):
    return datetime.fromisoformat(value) if value else None


def parse_number(text):
    """'1,500 €/mēn.' → 1500.0, '7.32 €' → 7.32, '52' → 52.0; None if none."""
    match = re.search(r"\d[\d,\s]*(?:\.\d+)?", text or "")
    if not match:
        return None
    return float(re.sub(r"[,\s]", "", match.group()))


def to_int(text):
    text = (text or "").strip()
    return int(text) if text.isdigit() else None


def price_unit(text):
    """'450 €/mēn.' → 'mēn', '28 €/dienā' → 'dienā', '98,000 €' → ''."""
    match = re.search(r"€\s*/\s*([^\s.]+)", text or "")
    return match.group(1) if match else ""


def district_name(district, locative=False):
    names = DISTRICTS.get(district, (district, district))
    return names[locative]


# --- Fetching -----------------------------------------------------------------


def decode(body, charset):
    """Page bytes as text, in the charset the server or the page names."""
    if not charset:
        match = re.search(rb"charset=[\"']?([\w-]+)", body[:4096], re.IGNORECASE)
        charset = match.group(1).decode("ascii") if match else "utf-8"
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


class Client:
    """Fetches ss.com pages one at a time, REQUEST_GAP apart."""

    def __init__(self):
        self.last = 0.0

    def get(self, url):
        """Return the page's text, or None if ss.com has no such page (404).

        Now and then a request times out, is cut off or gets a 5xx; the same
        request a little later goes through. Other HTTP errors fail straight
        away, since a retry won't fix them.
        """
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept-Language": "lv"}
        )
        for attempt in range(1, ATTEMPTS + 1):
            time.sleep(max(0.0, self.last + REQUEST_GAP - time.monotonic()))
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    body = response.read()
                    charset = response.headers.get_content_charset()
                return decode(body, charset)
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    return None
                transient = error.code >= 500 or error.code == 429
                if not transient or attempt == ATTEMPTS:
                    raise
                reason = f"HTTP {error.code}"
            except (http.client.HTTPException, OSError) as error:
                if attempt == ATTEMPTS:
                    raise
                reason = type(error).__name__
            finally:
                self.last = time.monotonic()
            wait = RETRY_WAIT * 2 ** (attempt - 1)
            print(
                f"::warning::{url} failed ({reason}); "
                f"retry {attempt} of {ATTEMPTS - 1} in {wait} s"
            )
            time.sleep(wait)


# --- Listing pages --------------------------------------------------------------


class Rows(HTMLParser):
    """Every table row on a page: its id, cell texts, links and images.

    ss.com marks its ad rows tr_<ad ID> and its header row head_line. Rows of
    the layout tables around the listing come out mangled, which is harmless:
    only the rows inside the listing are used.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.row = None
        self.cell = None
        self.hidden = 0  # depth inside <script> and <style>

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "style"):
            self.hidden += 1
        elif tag == "tr":
            self.end_row()
            self.row = {
                "id": attrs.get("id") or "",
                "cells": [],
                "links": [],
                "images": [],
            }
        elif self.row is None:
            return
        elif tag in ("td", "th"):
            self.end_cell()
            self.cell = []
        elif tag == "a" and attrs.get("href"):
            self.row["links"].append(attrs["href"])
        elif tag == "img" and (attrs.get("data-src") or attrs.get("src")):
            self.row["images"].append(attrs.get("data-src") or attrs["src"])
        elif tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(self.hidden - 1, 0)
        elif tag in ("td", "th"):
            self.end_cell()
        elif tag in ("tr", "table"):
            self.end_row()

    def handle_data(self, data):
        if self.cell is not None and not self.hidden:
            self.cell.append(data)

    def end_cell(self):
        if self.row is not None and self.cell is not None:
            self.row["cells"].append(squash("".join(self.cell)))
        self.cell = None

    def end_row(self):
        self.end_cell()
        if self.row is not None:
            self.rows.append(self.row)
        self.row = None

    def close(self):
        super().close()
        self.end_row()


def header_columns(rows):
    """The listing's data columns, left to right, from its header row.

    That's the row marked head_line, failing which the row naming the most
    columns; the search form's labels end in a colon, so they don't count.
    """
    headers = []
    for row in rows:
        keys = (re.sub(r"[\s,.]", "", cell.casefold()) for cell in row["cells"])
        names = [COLUMNS[key] for key in keys if key in COLUMNS]
        if {"rooms", "price"} <= set(names) and len(names) >= 4:
            headers.append((row["id"] == "head_line", len(names), names))
    return max(headers)[2] if headers else DEFAULT_COLUMNS


def parse_listing(page, section):
    """The ads on one listing page, top to bottom."""
    if not page:
        return []
    parser = Rows()
    parser.feed(page)
    parser.close()
    columns = header_columns(parser.rows)
    ads = []
    for row in parser.rows:
        link = next((link for link in row["links"] if "/msg/" in link), None)
        if not link or len(row["cells"]) <= len(columns):
            continue
        match = AD_ROW.match(row["id"])
        url = urllib.parse.urljoin(SITE, link)
        values = dict(zip(columns, row["cells"][-len(columns) :]))
        ads.append(
            {
                "id": match.group(1) if match else urllib.parse.urlsplit(url).path,
                "url": url,
                "deal": section.deal,
                "district": section.district,
                "title": max(row["cells"][: -len(columns)], key=len),
                "street": values.get("street", ""),
                "rooms": to_int(values.get("rooms")),
                "area": parse_number(values.get("area")),
                "floor": values.get("floor", ""),
                "series": values.get("series", ""),
                "price": parse_number(values.get("price")),
                "price_unit": price_unit(values.get("price")),
                "thumb": next(
                    (src for src in row["images"] if "/gallery/" in src), None
                ),
            }
        )
    return ads


def read_section(client, section, seen, read_all, min_pages):
    """The section's listing pages, newest first, each a list of ads.

    An ordinary run stops at the first page whose last ad was seen before:
    the ads past it are older still. Reading in full goes to the last page,
    which ss.com marks by showing an earlier page again, or by a 404.
    """
    pages, ids = [], set()
    for number in range(1, (MAX_SCAN_PAGES if read_all else MAX_PAGES) + 1):
        url = section.url + ("" if number == 1 else f"page{number}.html")
        ads = parse_listing(client.get(url), section)
        fresh = [ad for ad in ads if ad["id"] not in ids]
        if not fresh:
            break
        ids.update(ad["id"] for ad in fresh)
        pages.append(fresh)
        if not read_all and number >= min_pages and fresh[-1]["id"] in seen:
            break
    return pages


# --- Ad pages -------------------------------------------------------------------


class AdPage(HTMLParser):
    """An ad page's text, and the ad's own text from its msg_div_msg block.

    The fact table inside that block (Pilsēta, Iela, Cena …) is left out of
    the ad's text. Only <div> tags count towards the block's end, so an
    unclosed <p> or <b> in an ad can't keep it open.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.page = []
        self.ad = []
        self.hidden = 0  # depth inside <script> and <style>
        self.divs = 0  # <div> depth inside msg_div_msg, 0 outside it
        self.tables = 0  # tables open inside msg_div_msg

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        elif tag == "div" and self.divs:
            self.divs += 1
        elif tag == "div" and dict(attrs).get("id") == "msg_div_msg":
            self.divs = 1
        elif tag == "table" and self.divs:
            self.tables += 1
        if tag in ("br", "p", "div", "tr", "li"):
            self.add("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(self.hidden - 1, 0)
        elif tag == "div" and self.divs:
            self.divs -= 1
        elif tag == "table" and self.tables:
            self.tables -= 1
        if tag in ("p", "div", "tr", "li"):
            self.add("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.add(data)

    def add(self, text):
        self.page.append(text)
        if self.divs and not self.tables:
            self.ad.append(text)


def fallback_text(page_text):
    """The ad's text cut from the whole page's: after the map link (or the
    link back to the list) and before the fact table, which opens with
    "Pilsēta:"."""
    end = page_text.find("Pilsēta:")
    starts = [
        page_text.rfind(marker, 0, end) + len(marker)
        for marker in TEXT_START
        if end >= 0 and page_text.rfind(marker, 0, end) >= 0
    ]
    return page_text[max(starts) : end].strip() if starts else ""


def parse_ad_page(page):
    """When the ad was published (Riga time, ISO) and its own text."""
    parser = AdPage()
    parser.feed(page)
    parser.close()
    page_text = clean_text("".join(parser.page))
    text = clean_text("".join(parser.ad)) or fallback_text(page_text)
    match = DATE.search(squash(page_text))
    published = None
    if match:
        day, month, year, hour, minute = map(int, match.groups())
        published = datetime(year, month, day, hour, minute, tzinfo=RIGA)
        published = published.isoformat()
    return {"published": published, "text": text}


def utilities_included(text):
    """True if the ad says its rent includes utilities, False if it says they
    come on top, None if it says neither."""
    folded = fold(text)
    if any(pattern.search(folded) for pattern in UTILITIES_EXTRA):
        return False
    if any(pattern.search(folded) for pattern in UTILITIES_INCLUDED):
        return True
    return None


def utilities_note(text, limit=240):
    """What the ad says about utilities: its first two sentences on them."""
    sentences = (squash(part) for part in SENTENCE_BREAK.split(text or ""))
    picked = [sentence for sentence in sentences if UTILITY_WORDS.search(sentence)]
    return shorten(" ".join(picked[:2]), limit)


def add_details(client, ad):
    """Fill in what only the ad's own page says: when it was published, its
    text and what it says about utilities. False if the ad is gone already."""
    try:
        page = client.get(ad["url"])
    except (http.client.HTTPException, OSError) as error:
        print(f"::warning::could not read {ad['url']} ({error}); listing data only")
        return True
    if page is None:
        return False
    details = parse_ad_page(page)
    ad["published"] = details["published"]
    ad["text"] = shorten(squash(details["text"]), SNIPPET)
    ad["utilities_included"] = utilities_included(details["text"])
    ad["utilities_note"] = utilities_note(details["text"])
    return True


# --- Filters --------------------------------------------------------------------


def utilities_estimate(ad, rent):
    """Estimated utilities for a month, to the nearest 5 €."""
    area = ad["area"] or 20 * ad["rooms"]
    cost = rent["utilities_base"] + rent["utilities_per_m2"] * area
    return 5 * round(cost / 5)


def monthly_total(ad, rent):
    if ad.get("utilities_included"):
        return ad["price"]
    return ad["price"] + utilities_estimate(ad, rent)


def listing_match(ad, config):
    """Could the ad reach the feed? Before its own page is read, a rent ad
    only needs a monthly rent within the total."""
    if ad["rooms"] is None or ad["rooms"] < config["min_rooms"]:
        return False
    if ad["price"] is None:
        return False
    if ad["deal"] == "sell":
        return ad["price"] <= config["sale"]["max_price"]
    monthly = ad["price_unit"].startswith("mēn")
    return monthly and ad["price"] <= config["rent"]["max_total"]


def feed_match(ad, config):
    """Does the ad belong in the feed, utilities counted?"""
    if not listing_match(ad, config):
        return False
    rent = config["rent"]
    return ad["deal"] == "sell" or monthly_total(ad, rent) <= rent["max_total"]


def flat_key(ad):
    """Ads with the same key count as one flat posted more than once."""
    area = round(ad["area"]) if ad["area"] else None
    return ad["deal"], fold(ad["street"]), ad["rooms"], area, fold(ad["floor"])


def earlier_price(ad, items):
    """The price the ad's flat had when it was last in the feed, if it was."""
    same = [item for item in items if flat_key(item) == flat_key(ad)]
    if not same:
        return None
    return max(same, key=lambda item: item["first_seen"])["price"]


# --- Feed -----------------------------------------------------------------------


def euros(value):
    """98000 → '98 000 €', with no-break spaces as Latvian digit grouping."""
    return f"{value:,.0f}".replace(",", " ") + " €"


def decimal(value):
    """8.075 → '8,08' (Latvian decimal comma)."""
    return f"{value:.2f}".replace(".", ",")


def area_text(area):
    return f"{area:g}".replace(".", ",") if area else "?"


def rooms_text(rooms):
    word = "istaba" if rooms % 10 == 1 and rooms % 100 != 11 else "istabas"
    return f"{rooms} {word}"


def riga_time(value):
    return parse_time(value).astimezone(RIGA).strftime("%d.%m.%Y %H:%M")


def item_title(ad, rent):
    if ad["deal"] == "sell":
        price = f"Pārdod {euros(ad['price'])}"
    elif ad.get("utilities_included"):
        price = f"Izīrē {euros(ad['price'])} (ar komunālajiem)"
    else:
        total = euros(monthly_total(ad, rent))
        price = f"Izīrē {euros(ad['price'])} (~{total} ar komunālajiem)"
    if ad.get("earlier_price"):
        price += f", agrāk {euros(ad['earlier_price'])}"
    size = f"{ad['rooms']} ist., {area_text(ad['area'])} m²"
    place = f"{ad['street']}, {district_name(ad['district'])}"
    return " · ".join([price, size, place])


def describe(ad, rent):
    """HTML body of one feed item."""
    lines = []
    if ad.get("thumb"):
        lines.append(f'<img src="{html.escape(ad["thumb"])}" alt="">')
    place = f"{ad['street']}, {district_name(ad['district'])}"
    lines.append(f"<b>{html.escape(place)}</b>")
    facts = [rooms_text(ad["rooms"]), f"{area_text(ad['area'])} m²"]
    facts += [f"stāvs {ad['floor']}"] if ad["floor"] else []
    facts += [ad["series"]] if ad["series"] else []
    lines.append(html.escape(" · ".join(facts)))
    per_m2 = ad["price"] / ad["area"] if ad["area"] else None
    if ad["deal"] == "sell":
        line = f"Cena {euros(ad['price'])}"
        lines.append(line + (f" ({euros(per_m2)}/m²)" if per_m2 else ""))
    else:
        line = f"Īres maksa {euros(ad['price'])} mēnesī"
        lines.append(line + (f" ({decimal(per_m2)} €/m²)" if per_m2 else ""))
        if ad.get("utilities_included"):
            lines.append(
                "Komunālie maksājumi iekļauti īres maksā (tā teikts sludinājumā)"
            )
        else:
            lines.append(
                f"Komunālie maksājumi ~{euros(utilities_estimate(ad, rent))} "
                f"mēnesī (aplēse pēc platības), "
                f"kopā ~{euros(monthly_total(ad, rent))}"
            )
        if ad.get("utilities_note"):
            note = html.escape(ad["utilities_note"])
            lines.append(f"No sludinājuma: „{note}“")
    if ad.get("earlier_price"):
        lines.append(f"Šis dzīvoklis barotnē jau bija par {euros(ad['earlier_price'])}")
    if ad.get("published"):
        lines.append(f"Publicēts {riga_time(ad['published'])}")
    else:
        lines.append(f"Pirmoreiz redzēts {riga_time(ad['first_seen'])}")
    text = ad.get("text") or ad.get("title")
    if text:
        lines.append(f"<i>{html.escape(text)}</i>")
    return "<br>".join(lines)


def build_feed(items, config, now):
    """Render the feed, newest first."""
    rent = config["rent"]
    entries = []
    for ad in items:
        pub_date = format_datetime(parse_time(ad.get("published") or ad["first_seen"]))
        entries.append(
            "<item>"
            f"<title>{escape(item_title(ad, rent))}</title>"
            f"<link>{escape(ad['url'])}</link>"
            f'<guid isPermaLink="false">sscom-{escape(ad["id"])}</guid>'
            f"<pubDate>{pub_date}</pubDate>"
            f"<description>{escape(describe(ad, rent))}</description>"
            "</item>"
        )
    # The newest item's time, not the run's, so a run that finds nothing new
    # leaves the file as it was.
    built = max((parse_time(ad["first_seen"]) for ad in items), default=now)
    places = " un ".join(district_name(d, locative=True) for d in config["districts"])
    summary = (
        f"Vismaz {config['min_rooms']} istabas; īre līdz "
        f"{euros(rent['max_total'])} mēnesī ar komunālajiem vai cena līdz "
        f"{euros(config['sale']['max_price'])}"
    )
    link = DISTRICT_URL.format(district=config["districts"][0])
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">'
        "<channel>"
        f"<title>{escape(f'ss.com · jauni dzīvokļi {places}')}</title>"
        f"<link>{escape(link)}</link>"
        f'<atom:link href="{FEED_URL}" rel="self" type="application/rss+xml"/>'
        f"<description>{escape(summary)}</description>"
        "<language>lv</language>"
        f"<lastBuildDate>{format_datetime(built)}</lastBuildDate>"
        + "\n".join(entries)
        + "</channel></rss>\n"
    )


# --- State ----------------------------------------------------------------------


def load_config(path):
    with path.open("rb") as file:
        return tomllib.load(file)


def load_seen(path):
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as file:
        return {row["id"]: row["first_seen"] for row in csv.DictReader(file)}


def save_seen(path, seen):
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["id", "first_seen"])
        writer.writerows(sorted(seen.items(), key=lambda pair: (pair[1], pair[0])))


def load_json(path, default):
    return json.loads(path.read_text("utf-8")) if path.exists() else default


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", "utf-8")


def check_health(seen, now):
    """Return what looks wrong overall, one line per problem."""
    newest = max((parse_time(first_seen) for first_seen in seen.values()), default=None)
    if newest and now - newest > timedelta(days=MAX_QUIET_DAYS):
        return [
            f"No new ad since {newest.astimezone(RIGA):%d.%m.%Y %H:%M}, over "
            f"{MAX_QUIET_DAYS} days; ss.com usually adds dozens a day"
        ]
    return []


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=Path, default=Path("site"))
    parser.add_argument(
        "--backfill-pages",
        type=int,
        default=0,
        help="also put the matching ads from the first N listing pages of "
        "each section into the feed",
    )
    args = parser.parse_args()

    config = load_config(CONFIG_FILE)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    stamp = now.isoformat()
    site = args.site
    seen = load_seen(site / "seen.csv")
    scanned = set(load_json(site / "state.json", {"scanned": []})["scanned"])
    items = {item["id"]: item for item in load_json(site / "items.json", [])}

    client = Client()
    problems, candidates, new_ids = [], {}, []
    for section in (Section(d, deal) for d in config["districts"] for deal in DEALS):
        read_all = section.key not in scanned
        pages = read_section(
            client, section, seen, read_all, max(args.backfill_pages, 1)
        )
        first = pages[0] if pages else []
        readable = sum(
            ad["price"] is not None and ad["rooms"] is not None for ad in first
        )
        if not first or readable < len(first) / 2:
            problems.append(
                f"{section.url}: {readable} of {len(first)} ads on the first "
                "page have rooms and a price I can read; has ss.com changed "
                "its page layout?"
            )
            continue
        for number, ads in enumerate(pages, start=1):
            for ad in ads:
                fresh = ad["id"] not in seen
                if fresh:
                    seen[ad["id"]] = stamp
                    new_ids.append(ad["id"])
                # A section read in full for the first time offers only its
                # first page, so the feed doesn't open with weeks of ads.
                wanted = number <= args.backfill_pages or (
                    number == 1 if read_all else fresh
                )
                if wanted and ad["id"] not in items and listing_match(ad, config):
                    candidates[ad["id"]] = ad
        scanned.add(section.key)
        print(
            f"{section.key}: {len(pages)} page(s), "
            f"{sum(len(ads) for ads in pages)} ads"
            + (" (read in full)" if read_all else "")
        )

    added = reposts = gone = 0
    for ad in candidates.values():
        if not add_details(client, ad):
            gone += 1
            continue
        if not feed_match(ad, config):
            continue
        ad["first_seen"] = stamp
        ad["earlier_price"] = earlier_price(ad, items.values())
        if ad["earlier_price"] is not None and ad["price"] >= ad["earlier_price"]:
            reposts += 1
            continue
        items[ad["id"]] = ad
        added += 1

    def age(item):
        return now - parse_time(item["first_seen"])

    kept = sorted(
        (item for item in items.values() if age(item) <= timedelta(REPOST_DAYS)),
        key=lambda item: (item["first_seen"], item.get("published") or ""),
        reverse=True,
    )
    # Config changes apply to the ads already in the feed too.
    shown = [
        item
        for item in kept
        if age(item) <= timedelta(FEED_DAYS) and feed_match(item, config)
    ]

    site.mkdir(parents=True, exist_ok=True)
    save_seen(site / "seen.csv", seen)
    save_json(site / "state.json", {"scanned": sorted(scanned)})
    save_json(site / "items.json", kept)
    (site / "feed.xml").write_text(build_feed(shown, config, now), encoding="utf-8")
    print(
        f"New ads: {len(new_ids)} | matching, checked: {len(candidates)} | "
        f"added: {added} | posted again: {reposts} | gone: {gone} | "
        f"in feed: {len(shown)}"
    )
    problems += check_health(seen, now)
    PROBLEMS_FILE.write_text("".join(f"{line}\n" for line in problems), "utf-8")


if __name__ == "__main__":
    main()
