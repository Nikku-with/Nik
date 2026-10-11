---
name: website_data_collection
description: "Alag-alag websites se structured data collect karke note me save karne ka standard workflow"
keywords: [website, sites, scrape, prices, compare, research, data, note, save, collect, "web se", "site se"]
tools: [browser_research, browser_task, google_search, deep_research, save_note, find_files]
---

# Skill: Multi-site data collection (owner ka favourite workflow)

Jab owner bole *"jaao browser pr alag alag websites se XYZ laao aur notepad me ABC naam rakh ke save kardo"*,
to ye exact sequence follow karo:

## Steps

1. **Sites identify karo** (agar owner ne nahi diye):
   - India ke liye: Amazon.in, Flipkart, Croma, 91mobiles, GSMArena (category ke hisaab se)
   - 3–5 sites kaafi hain. Zyada sites = zyada time, kam value.
2. **Pehle sasta rasta try karo** → `deep_research(query, sites=[...])`
   - Ye parallel HTTP fetch karta hai (seconds me). JS-heavy/bot-protected ho to agla step.
3. **Interaction chahiye ho** (pagination, "Load more", login, filters) → `browser_research(task, sites=[...], fields=[...], save_note_as="<name>")`
   - Har site ke liye alag browser agent parallel chalta hai.
   - `fields` me exact names do: `["product","price","rating","delivery"]`
4. **Normalize karo** (model/price ko comparable banao — storage, variant, currency same karo).
5. **Save karo** `save_note(name="<owner ka diya naam>")` — content me:
   - top picks table, har value ke saath source URL, aur "as of <date>" line
6. **Owner ko short Hinglish summary** do: kitne sites se aaya, top 3 highlights, note ka path.

## Rules

- Kuch bhi **invent mat karo** — jo page par tha wahi likho, warna `null`.
- Price/stock jaisi cheez "as of <date>" likhe bina mat bhejo.
- Owner ne note ka naam diya ho to **wahi naam** use karo (dusra mat banao).
- Ek site fail ho to baaki ka data bhejo aur clearly bolo kaunsi site fail hui.
