"""Dev/test-only helper: deletes a site_members row by email so a claim flow
can be re-tested repeatedly without tripping site_members_email_uq.

NOT a prod code path — never imported by app/*. Run directly:

    APP_ENV=development python scripts/reset_test_member.py TEST_alice@example.com

Guarded twice:
  1. Refuses to run unless APP_ENV=development (same convention as
     app/config.py's settings.app_env / main.py's dev-only CORS switch).
  2. Refuses any email that doesn't start with "TEST_" (case-insensitive),
     so a typo can't delete a real member row.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.dependencies.supabase_client import get_supabase_admin_client  # noqa: E402

TEST_EMAIL_PREFIX = "test_"


def main() -> None:
    app_env = os.environ.get("APP_ENV", "production").strip().lower()
    if app_env != "development":
        print("Refusing: APP_ENV must be 'development' (got %r)." % app_env, file=sys.stderr)
        sys.exit(1)

    if len(sys.argv) != 2:
        print("Usage: python scripts/reset_test_member.py TEST_<email>", file=sys.stderr)
        sys.exit(1)

    email = sys.argv[1].strip().lower()
    if not email.startswith(TEST_EMAIL_PREFIX):
        print(f"Refusing: email must start with '{TEST_EMAIL_PREFIX}' (got {email!r}).", file=sys.stderr)
        sys.exit(1)

    client = get_supabase_admin_client()
    deleted = client.table("site_members").delete().eq("email", email).execute().data
    print(f"Deleted {len(deleted)} site_members row(s) for {email}.")


if __name__ == "__main__":
    main()
