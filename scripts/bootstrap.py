"""Provision the MDM schemas, metadata tables and break-glass admin.

Idempotent: safe to run on every deploy. Reports precisely which grant is
missing rather than surfacing a raw driver error.

    python -m scripts.bootstrap [--create-database] [--seed]
"""
import argparse
import sys

from app.config import settings
from app.db import check_connection, session_scope
from app.services.auth import ensure_local_admin
from app.services.bootstrap import bootstrap


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--create-database", action="store_true",
                    help="CREATE DATABASE first (needs the CREATEDB privilege).")
    ap.add_argument("--seed", action="store_true",
                    help="Also load the example data models (unpublished drafts).")
    args = ap.parse_args()

    conn = check_connection()
    if not conn.get("connected") and not args.create_database:
        print(f"Cannot reach PostgreSQL at {settings.PGHOST}:{settings.PGPORT}"
              f"/{settings.PGDATABASE}\n  {conn.get('error')}", file=sys.stderr)
        print("\nCheck PGHOST/PGPORT/PGUSER/PGPASSWORD, or pass "
              "--create-database if the database does not exist yet.",
              file=sys.stderr)
        return 2

    print(f"Provisioning {settings.PGDATABASE} on "
          f"{settings.PGHOST}:{settings.PGPORT}")
    try:
        result = bootstrap(create_db=args.create_database)
    except PermissionError as exc:
        print(f"\nInsufficient privileges:\n  {exc}", file=sys.stderr)
        return 3

    if "database" in result:
        d = result["database"]
        print(f"  database: {'created' if d.get('created') else d.get('reason')}")
    print(f"  schemas:  {', '.join(result['schemas'])}")
    print(f"  metadata: {len(result['metadata_tables'])} tables")

    privs = result["privileges"]
    print(f"  ddl role: {privs.get('ddl_user')}")
    problems = [c for c in privs["checks"] if not c["ok"] and c.get("remedy")]
    for c in problems:
        print(f"    ! {c['name']}: {c['detail']}\n      fix: {c['remedy']}")

    with session_scope() as db:
        admin = ensure_local_admin(db)
    if admin and admin.get("created"):
        pw = admin.get("generated_password")
        print(f"  admin:    created '{admin['username']}'")
        if pw:
            print(f"\n  Break-glass password (shown once): {pw}\n")
    elif admin:
        print(f"  admin:    '{admin['username']}' already exists")

    if args.seed:
        from scripts.seed import seed_models
        for line in seed_models():
            print(f"  seed:     {line}")

    print("\nDone." + ("" if not problems else
                       " Address the privilege warnings above before publishing."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
