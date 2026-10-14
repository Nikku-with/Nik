---
name: gst_invoice
description: "Client ke liye GST invoice banane ka process (HTML → PDF), record ke saath"
keywords: [invoice, bill, gst, client, payment, billing, quotation]
tools: [read_file, code_engine, save_note, write_file, run_shell_command]
---

# Skill: GST invoice / bill banao

1. **Client ka data nikaalo** — `clients.csv` / `read_file("clients.csv")` se naam, GSTIN, address.
   Na mile to owner se poocho (guess mat karo).
2. **Line items confirm karo** — description, qty, rate, GST% (owner se ya `items.json` se).
3. **Invoice generate karo** — `code_engine` se ek Python script likhwao jo:
   - HTML invoice banaye (invoice no, date, GSTIN, HSN/SAC, CGST/SGST split, total in words)
   - usko `workspace/artifacts/invoice_<client>_<date>.html` me save kare
   - optional: `weasyprint`/`wkhtmltopdf` ho to PDF bhi (`run_shell_command`)
4. **Verify karo** — totalling dobara calculate karke HTML ke total se match karao (mismatch ho to fix).
5. **Record rakho** — `save_note(name="invoice_register", append=True)` me entry: date, client, amount, file path.
6. **Owner ko batao**: file path + total + GST amount (1-2 line Hinglish).

## Rules
- GSTIN format validate karo (15 chars, `\d{2}[A-Z]{5}\d{4}[A-Z]\d[Z][A-Z\d]`).
- Invoice number sequence maintain karo (register se next number lo).
- Amount in words bhi likho (Indian numbering: lakh/crore).
