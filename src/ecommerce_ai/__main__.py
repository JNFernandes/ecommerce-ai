"""``python -m ecommerce_ai [serve|migrate]``: serve the app, or apply the database migrations."""

import sys

from ecommerce_ai.main import cli

if __name__ == "__main__":
    sys.exit(cli())
