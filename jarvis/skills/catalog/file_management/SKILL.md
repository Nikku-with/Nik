---
name: file_management
description: "Files dhoondhna, padhna, organize karna aur gated writes ko verify karna"
keywords: [file, files, folder, directory, document, pdf, csv, read, search, rename, delete, save]
tools: [find_files, find_file, list_dir, search_inside_files, read_file, write_file, folder_file, undo_file_operation, recent_files, save_note, read_note, list_notes, search_notes]
---

# File workflow

1. `find_files` / `find_file` se likely path dhoondo; exact path unclear ho to `list_dir` se candidates check karo.
2. `read_file` se file type ke hisaab se content padho; `search_inside_files` large folders me targeted text ke liye use karo. Summary me sirf observed content bolo.
3. New file create karne se pehle output path aur format confirm karo. `write_file` existing non-empty file ko replace karne se pehle owner Gate leta hai; deny/timeout par original file unchanged rehni chahiye.
4. Move/delete/rename jaise operations ke liye `folder_file` use karo. Exact target aur effect samjhao; delete Gate approval ke bina mat karo. Recent file-manager changes can be reversed with `undo_file_operation` or `folder_file("undo last file operation")`; undo refuses if the target changed afterward, and removal/replacement still needs owner approval.
5. Write ke baad file ko dobara read ya stat karke verify karo, phir path report karo.

## Notes

`list_notes`, `read_note`, `search_notes`, aur `save_note` note-specific workflows ke liye hain. Existing non-empty note overwrite par approval chahiye. Search ya indexing fail ho to “file nahi hai” ka daava mat karo.
