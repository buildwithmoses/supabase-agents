"""Courtline support agent with per-customer memory in Postgres (fictional data).

Postgres decides who sees which memories (RLS on auth.uid()); code decides what is worth remembering and
what must never be stored; the model proposes memories and writes replies; money waits for Nadia."""

SCHEMA = "support"
