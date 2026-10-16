#!/usr/bin/env bash
# Sandbox/dev bootstrap — core runtime deps (browser-use/livekit bhaari hain, wo optional).
# Usage: bash scripts/dev_install.sh
set -e
pip3 install -q --disable-pip-version-check \
  "langchain-core>=0.3" "langchain>=0.3" "langgraph>=0.2" "langchain-openai>=0.2" \
  "dspy>=3.0" "pydantic>=2.6" "pydantic-settings>=2.2" "python-dotenv>=1.0" \
  "httpx>=0.27" "requests>=2.31" "beautifulsoup4>=4.12" "lxml>=5.0" "ddgs>=9.0" \
  "pypdf>=5.0" "python-docx>=1.1" "openpyxl>=3.1" "python-pptx>=1.0" "Pillow>=10.0" \
  "rapidfuzz>=3.9" "rich>=13.7" "fastapi>=0.110" "uvicorn[standard]>=0.30" \
  "python-telegram-bot>=21.0" > /dev/null 2>&1 || pip3 install --disable-pip-version-check \
  "langchain-core>=0.3" "langchain>=0.3" "langgraph>=0.2" "langchain-openai>=0.2" \
  "dspy>=3.0" "pydantic>=2.6" "pydantic-settings>=2.2" "python-dotenv>=1.0" \
  "httpx>=0.27" "requests>=2.31" "beautifulsoup4>=4.12" "lxml>=5.0" "ddgs>=9.0" \
  "pypdf>=5.0" "python-docx>=1.1" "openpyxl>=3.1" "python-pptx>=1.0" "Pillow>=10.0" \
  "rapidfuzz>=3.9" "rich>=13.7" "fastapi>=0.110" "uvicorn[standard]>=0.30" \
  "python-telegram-bot>=21.0" 2>&1 | tail -3
python3 - <<'PY'
import importlib
missing = [m for m in ("langchain_core","langgraph","dspy","rich","fastapi","uvicorn","ddgs","telegram")
           if not _ok(m)] if False else []
for m in ("langchain_core","langgraph","dspy","rich","fastapi","uvicorn","ddgs","telegram","httpx","rapidfuzz"):
    try:
        importlib.import_module(m)
    except Exception:
        missing.append(m)
print("core deps:", "OK ✅" if not missing else f"MISSING {missing}")
PY
