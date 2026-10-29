"""Docs Q&A: Courtline's help center in Postgres (pgvector + full text, RLS by audience), answered with
verified citations. See docs/docs-qa.md."""

PLAYER_ID = "11111111-1111-4111-8111-111111111111"   # a member (player) at a Courtline club
STAFF_ID = "22222222-2222-4222-8222-222222222222"    # club front-desk staff
USERS = {"player": PLAYER_ID, "staff": STAFF_ID}
WHO_TO_ASK = {
    "player": "your club's front desk, or Courtline support at support@courtline.example (in-app chat 8 am to 8 pm Eastern, Monday to Saturday)",
    "staff": "Courtline club success at clubs@courtline.example",
}
