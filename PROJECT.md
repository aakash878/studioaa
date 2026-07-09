# HQ — project state · July 10, 2026

The working prototype is `american-style-drop.html`. One self-contained file, open in Chrome full screen. All behavior verified by two automated suites (46 checks, real clicks and keystrokes, zero runtime errors).

## What this is
A one-page project HQ for creatives. Replaces Notion/Sheets for running a project. Demonstrated on the real American Style merch drop (100 units, $1,958 landed, break even 56, 28-person seeding list, XL gap flagged automatically).

## Design laws (settled)
One page, ever — depth opens in place, never navigates away. One tile, one signal — numbers for glancing, charts for understanding, prose almost never. Signals center, sequences left-align, order is always visible (01/02/03 chips everywhere). The page sheds what's resolved and calms when clear. No dotted or dashed lines anywhere. Inter only, bold baseline (700), monochrome with glass; attention is weight and motion, never color. Everything enters like the title: rise, blur-to-sharp.

## The arrival (in order)
Reel ("we can assist with…" decelerating into "your next project", click to skip) → Welcome. → type what you're making (free text, architecture inferred: drop/release/event/client, else generic) → Name it → Build your architecture (chips, toggle, add your own) → Pick your light (Weather/Still/Ink, applied live) → Enter Aakash HQ → title flies to the top, tiles assemble with birth-reason tags → sidebars peek once. Onboarding is one-time; Config panel (left) holds the answers, editable forever.

## The HQ
Browser-grammar top: tabs merge into a chrome bar, + tab = new page (Notion-style: name it, land on a blank canvas of + blocks, compose with /table, /checklist, /money, /dates and type-to-track; blocks land on the page they were typed on). A stagnant cover compartment sits above the title, video-capable: hover it, paste an mp4/webm URL, it loops silently; default is a slow dark gradient field. Preset lens tabs are gone from the rail; lenses open through their tiles (tiles are doors, verified). Onboarding is two breaths only: what are you making, pick your light. Enter lands on a blank canvas; the furnished drop tiles live behind Config toggles. Hero title as masthead. Asymmetric Apple-grade bento (28px radius, shadow depth, spring hover). Tiles: Now (checkable, ordered, sheds), On track? (verdict: Ahead/On track/Behind, done-vs-time bars), Finance (break-even chart), Inventory (size chart, XL zero), Seeding (progress ring), People. Tiles are doors: clicking opens the full lens in-page with ← Overview. Swappable previews (⇄ on hover). + slots grow the page; "type to track" parses language into Count/Progress/Checklist/Money/Dates tiles. Right sidebar: gliding chronology rail with today-tick, authored in setup. Whiteboard tab: think out loud, lines auto-file into buckets; /table spawns linked live databases (editable cells, +row/+col, tile updates live); /checklist /money /dates work too. Shipping tab: paste tracking numbers once → 28 progress bars to destinations. Notes: + Note in header, one click, untyped on purpose. Share: walkthrough travels with the recipient's first open only, per-role links, preview available.

## Ambience
JS-driven sky engine (requestAnimationFrame, immune to Reduce Motion): floating monochrome gradient base + five drifting masses. Live NYC weather (open-meteo, no key) steers tempo (wind), mood (cloud cover), night dim, rain streaks; footer shows current condition. Themes: Weather / Still (same sky, slow) / Ink (night glass). Haptics on interactions (mobile), toggle in Config. Failsafe: any runtime error strips overlays so the page can never brick.

## Real client math baked in (for Biz, due Friday Jul 10)
Lena Dunham needs XL; the run has zero XLs — decide with the style commit. Seeding 28 of 100 means break even = 56 of 72 sellable = 78% sell-through. Sell-out of the 72 nets $562; the $1,542 max assumes all 100 sell.

## Build system note
Variants (hq-preview, biz-hq) are regenerated from american-style-drop.html by a derive() function with assertions that halt on any failed text match. Root cause of three shipped bugs was silent text-replacement edits; moving to a real codebase (Claude Code) kills this class. Files: american-style-drop.html (journey), hq-demo.html (copy of journey for pitching), hq-preview.html (Aakash daily, furnished), biz-hq.html (flag sky, furnished), login.html (demo auth, hands off to hq-preview), all test suites in /home/claude of the old session.

## Open threads for tomorrow
Write the full concept spec (this file is the working summary). Real model calls behind: onboarding inference, type-to-track, whiteboard routing, tile walkthrough generation. Persistence (everything is in-memory). Multi-project tabs for real. Deploy: drag file (renamed index.html) into Netlify → shareable link; or reconnect Netlify/browser connector and Claude pushes it. Name the product.
