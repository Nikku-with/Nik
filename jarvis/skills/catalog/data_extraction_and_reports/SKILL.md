---
name: data_extraction_and_reports
description: "Website, PDF, Excel aur CSV se traceable normalized data nikaal kar reusable report banana"
keywords: [data extraction, report, pdf, excel, spreadsheet, csv, normalize, source, "data nikaalo", "report banao"]
tools: [google_search, fetch_page, browser_research, read_file, write_file, code_engine, run_script_file, save_note]
---

# Data extraction and reporting

1. Output schema pehle define karo: field names, units, date format, currency, missing-value convention, and source/date columns. User ke comparison criteria ko preserve karo; fields ko quietly change mat karo.
2. Website ke liye `google_search` se official pages locate karo, `fetch_page` se static pages verify karo, aur pagination/JavaScript ki zaroorat par `browser_research` use karo. PDF/Excel/CSV ke liye `read_file` se source contents aur relevant pages/sheets inspect karo.
3. Har extracted row/value ke saath source path/URL, page/sheet/row (available ho to), aur retrieval/publication date record karo. Search snippet alone source nahi; ambiguous, conflicting, or missing value ko `unknown` rakho.
4. Values normalize karo—same currency, units, date timezone, product variant, tax/delivery basis aur duplicate handling. Calculations use karne par formula/assumptions report me state karo; source values ko derived values se alag rakho.
5. Requested output ke hisaab se `write_file` se CSV, JSON, ya Markdown save karo. XLSX/PDF conversion ya non-trivial transforms chahiye hon to `code_engine`/`run_script_file` use karo; script/output ka actual return status aur produced file inspect karke hi format claim karo.
6. Saved report ko `read_file` / `find_files` se reopen karke row count, headings, sample values, and source references verify karo. User ne explicitly long-term note save karne ko kaha ho tabhi `save_note` use karo.
7. Hinglish me total sources/rows, retrieval date, output path, omissions, normalization assumptions, and any failed source report karo.

## Rules

- Missing fields infer mat karo; guessed values ko factual data ke roop me publish mat karo.
- Source text ke alawa confidence ya exactness ka unsupported claim mat karo.
- OCR/reader/parser failure ko zero data milna samajhkar “source empty” mat bolo; method failure disclose karo.
- Existing file replace karne ke liye approval/Gate ko follow karo. Search or extraction output me credentials/secrets mile to report me redact karo.
