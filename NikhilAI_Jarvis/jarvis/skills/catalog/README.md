# Skills (OpenClaw-style SKILL.md instruction packs)

Yahan apne **workflows** likho — Jarvis unhe padh kar waise hi kaam karega.
Tool nahi, **instructions**. Jo skill request se match karti hai, wahi supervisor/planner
ke prompt me inject hoti hai (poora catalog nahi — token bachta hai).

## Naya skill banane ka tarika

```
skills/<skill_naam>/SKILL.md
```

```markdown
---
name: invoice_banao
description: "Client invoice banane ka exact process"
keywords: [invoice, bill, gst, client, payment]
tools: [code_engine, save_note, read_file]
---

# Steps
1. client ka data `read_file("clients.csv")` se lo
2. code_engine se invoice PDF/HTML generate karwao
3. save_note me record rakho
```

* `keywords` → inhi par matching hoti hai (Hinglish words bhi likh sakte ho: "bill", "paisa")
* `tools` → optional, sirf hint hai (actual tools tumhare paas already hain)
* Body me **concrete sequence** likho: kaun sa tool, kis order me, kya verify karna hai

## Kaise check karein

```bash
python run_cli.py --tools          # catalog
python -c "from jarvis.skills.loader import catalog; print(catalog())"
```
Ya Jarvis se bolo: *"skills dikhao"* / *"invoice_banao skill padho"*.
