import os

# The suite shares one app and client IP; tests that exercise the limiter build their own app
os.environ["RATE_LIMIT_ENABLED"] = "false"
