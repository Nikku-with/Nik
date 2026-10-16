---
name: website_data_collection
description: "Kai websites se structured data collect, normalize, source aur save karne ka workflow"
keywords: [website, sites, scrape, prices, compare, research, data, note, save, collect, "web se", "site se"]
tools: [browser_research, browser_task, deep_research, google_search, fetch_page, save_note, find_files]
---

# Multi-site data collection

1. Owner ke diye sites use karo. Sites na di hon to `google_search` se relevant official/vendor pages dhoondo; 3–5 sites kaafi hoti hain.
2. Pehle `deep_research` ka HTTP path try karo. JavaScript, pagination ya interaction zaroori ho tab `browser_research` use karo; single interactive task ke liye `browser_task`.
3. `fields` ko exact aur comparable rakho (jaise product, variant, price, rating, delivery). Har row me source URL aur retrieval time rakho.
4. Values normalize karo—same variant, capacity, currency aur tax/delivery basis. Missing values ko `null`/unknown rakho; infer mat karo.
5. `save_note` se owner ke diye naam par report save karo. Existing note overwrite karne se pehle human Gate approval required hai.
6. Owner ko Hinglish me site-by-site success/failure, top findings, “as of” time, aur note path batao. Browser ke “page par mila” ko independent verification mat bolo.

## Rules

- Ek site fail ho to successful sites ka partial result do aur failed site ka naam batao.
- Login, purchase, posting, deletion, ya sensitive account change browser se karne se pehle explicit human approval lo.
- Browser stuck/timeout ho to retry budget use karke rok do; same state par anant retry nahi.
