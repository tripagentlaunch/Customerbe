"""Repo-wide test setup. The real .env has live Supabase credentials, so
API usage tracking is switched off for every test run — tests must never
write api_usage rows to the real database."""

import os

os.environ["API_USAGE_TRACKING"] = "off"
