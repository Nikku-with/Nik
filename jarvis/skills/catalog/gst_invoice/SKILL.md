---
name: gst_invoice
description: "Client ke liye GST invoice banane aur totals verify karne ka workflow"
keywords: [invoice, bill, gst, client, payment, billing, quotation]
tools: [find_files, read_file, code_engine, write_file, save_note]
---

# GST invoice workflow

1. `find_files` / `read_file` se client record aur line items dhoondo. GSTIN, address, item, quantity, rate, aur GST rate missing hon to owner se poochho; guess mat karo.
2. Invoice number, invoice date, HSN/SAC aur CGST/SGST ya IGST treatment confirm karo. In details ko source/owner input se hi bharo.
3. `code_engine` se HTML invoice generate karo aur `write_file` se `workspace/artifacts/invoice_<client>_<date>.html` me save karo. Existing file replace karni ho to Gate approval lo.
4. Totals ko independently dobara calculate karke subtotal, tax, rounding aur grand total verify karo. Mismatch ho to invoice ko final mat bolo.
5. `save_note` se invoice register me date, client, invoice ID, amount, tax, aur artifact path record karo; `save_note` me append flag nahi hota, isliye register content ko read karke naya complete version save karo.
6. Owner ko short Hinglish summary do: invoice path, total, GST amount, aur koi missing/assumed field.

## Checks

- GSTIN ko 15-character format ke against validate karo; invalid value par invoice rok do.
- Indian numbering me amounts dikhana; currency aur tax split ko input se verify karo.
- Invoice number duplicate mat karo. Record ya owner confirmation na ho to next number guess nahi karna.
- PDF conversion optional hai; sirf tab jab required converter registered/available ho. Failure ho to HTML result honestly report karo.
