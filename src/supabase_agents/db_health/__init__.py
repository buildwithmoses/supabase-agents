"""db-health: Supabase database health agent for Courtline (fictional).

Code finds problems (Splinter-style lints + pg_stat_statements + EXPLAIN), the model explains them,
code writes the fix as migration SQL and proves it in a shadow copy of the schema, and nothing touches the
real schema until @sam approves.
"""

SCHEMA = "health"
SHADOW_PREFIX = "health_shadow_"
APPROVER = "sam"
