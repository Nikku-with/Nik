---
name: tool_system_ops
description: "System maintenance — refresh/reload config, cache cleanup, restart Jarvis, machine status"
keywords: [refresh, reload, restart, reboot, cache, clean, clear, saaf, status, health, cpu, ram, disk, system, config, settings, machine]
tools: [refresh_system_tool, clear_cache_tool, reload_config_tool, restart_jarvis_tool, full_system_status]
---

# System ops tools

- full_system_status() — time, CPU, RAM, disk, uptime, workspace size, python version.
- refresh_system_tool(clear_caches) — config/tools/models dobara load ("refresh system").
- reload_config_tool() — sirf .env/settings reload.
- clear_cache_tool(scope, dry_run) — scope: all | python | logs | index. Pehle dry_run=True se dikhao, phir owner ki haan par actual run.
- restart_jarvis_tool() — controlled restart; same command se wapas aata hai.

## Rules
- Cache clear ya restart se pehle owner ko batao.
- Status ke liye full_system_status; sirf config ke liye reload_config_tool.
