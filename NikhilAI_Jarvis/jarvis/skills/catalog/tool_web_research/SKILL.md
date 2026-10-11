---
name: tool_web_research
description: "Internet search, web page padhna, deep research, current time (IST) aur weather"
keywords: [search, google, internet, web, website, link, url, research, khoj, dhoondo online, news, khabar, latest, weather, mausam, temperature, time, samay, date, tareekh, aaj, source]
tools: [google_search, fetch_page, deep_research, get_current_datetime, get_weather]
---

# Web & research tools (READ level)

- google_search(query, num) — top results (title, url, snippet). Pehla step.
- fetch_page(url) — ek page ka readable text, bina browser ke (fast). JS-heavy sites ke liye browser tools use karo.
- deep_research(query, sites, max_pages) — search + saare pages parallel fetch karke combined text. Gehri research ke liye.
- get_current_datetime() — IST me current date/time. Date/time kabhi andaaze se mat batao.
- get_weather(city) — city ka mausam; city khali ho to IP se detect.

## Steps
1. google_search → 2. fetch_page (2-3 best sources) → 3. answer me sources ke URL do.
Sources ke bina facts mat likho. Kuch fail ho to fallback engine khud try hota hai.
