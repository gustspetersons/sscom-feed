# sscom-feed

An RSS feed of new [ss.com](https://www.ss.com) flat ads in Rīga's Centrs and
Vecrīga: at least 2 rooms, and either a rent of up to 600 EUR a month with
utilities or a sale price of up to 150 000 EUR.

**Feed:** https://gustspetersons.github.io/sscom-feed/feed.xml

## How it works

Every 30 minutes a GitHub Actions job reads the newest listing pages of four
ss.com sections (Centrs and Vecrīga, Izīrē and Pārdod) and compares the ad IDs
with every ID seen before (`seen.csv` on the `gh-pages` branch). New ads that
fit the limits in `config.toml` go into the feed, which keeps each for 14 days.

- **Utilities:** ss.com's price is the rent. When the ad's text says the rent
  includes utilities, the rent is the total; otherwise the total is the rent
  plus an estimate of 50 EUR + 2 EUR per m² (~150 EUR for 50 m²). Each item
  shows the estimate and quotes what the ad says about utilities.
- **Rooms in shared flats** stay out: ads with under 10 m² a room, or worded
  as one ("Istaba Vecrīgā …", "izīrē istabu").
- **Bumped ads** (lifted back to the top) keep their ID and never come back.
- **Flats posted again** under a new ID (same deal, street, rooms, area and
  floor) stay out, unless the price is lower; the item then says what it was.
- **A new section** (a district added to `config.toml`) is first read in full,
  so its older ads don't turn up as new later; only its first page goes into
  the feed.

## Day to day

- **Limits:** edit `config.toml`. A push to `main` rebuilds the feed straight
  away, and the new limits apply to the ads already in it too.
- **Backfill:** Actions → Update feed → Run workflow, with `pages` set to N,
  adds the matching ads from the first N listing pages of each section.
- **If it breaks** (ss.com changes its pages), the run fails and GitHub emails
  a notice. So does a section whose first page has no readable ads, and a
  stretch of 2 days without a single new ad.
- **Staying on schedule:** GitHub turns schedules off in public repos after 60
  days without activity; every run re-enables the workflow to reset that.
