"""Create the database schema: python -m app.db.init"""

from app.db.conn import apply_schema, connect


def main() -> None:
    with connect() as conn:
        apply_schema(conn)
    print("Schema applied.")


if __name__ == "__main__":
    main()
