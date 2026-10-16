---
name: web_research
description: "Web se sourced research karna, dates/claims check karna, aur citations ke saath report dena"
keywords: [research, search, web, latest, current, news, weather, compare, source, cite]
tools: [google_search, deep_research, fetch_page, get_weather, get_current_datetime]
---

# Web research workflow

1. Query ko answerable subquestions me todo. Current/date-sensitive query ke liye `get_current_datetime` se local time reference lo.
2. `google_search` se authoritative sources dhoondo; `fetch_page` se source passages verify karo. Broad multi-site research ke liye `deep_research` use karo.
3. Har factual claim ke saath source title/URL aur publication/retrieval date do. Search snippet ko source page ke barabar na mano.
4. Sources disagree karein to dono positions aur dates report karo. Source unavailable, paywalled, stale, ya inconclusive ho to clearly bolo.
5. Weather request me `get_weather` use karo aur location/timezone confirm ya configured default (Kota/IST) apply karo; default guess ko disclose karo.

## Rules

- Live research unavailable ho to training knowledge se “latest” ka daava mat karo.
- Data ko source ke facts aur Jarvis ke interpretation me alag rakho.
- User ne save karne ko kaha ho tabhi `save_note` workflow use karo; otherwise answer inline do.
