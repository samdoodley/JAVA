from kiteconnect import KiteConnect
from dotenv import load_dotenv
import os

load_dotenv()

# Load API credentials from environment, or prompt if missing
api_key = os.getenv("KITE_API_KEY")
api_secret = os.getenv("KITE_API_SECRET")

if not api_key:
    api_key = input("Enter your KITE_API_KEY: ").strip()

if not api_secret:
    api_secret = input("Enter your KITE_API_SECRET: ").strip()

kite = KiteConnect(api_key=api_key)

# Step 1 - Generate login URL
print("Login here: ", kite.login_url())

# Step 2 - Get request token from the redirect URL after login
request_token = input("Paste your request token here: ").strip()

# Step 3 - Generate session and set access token
data = kite.generate_session(
    request_token,
    api_secret=api_secret
)
access_token = data["access_token"]
print("Access Token:", access_token)

# Save to a file for reuse
with open("access_token.txt", "w", encoding="utf-8") as f:
    f.write(access_token)

# Also save to .env so bot can load it automatically
env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.isfile(env_path):
    with open(env_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
else:
    lines = []

with open(env_path, "w", encoding="utf-8") as f:
    found = False
    for line in lines:
        if line.strip().startswith("KITE_ACCESS_TOKEN="):
            f.write(f"KITE_ACCESS_TOKEN={access_token}\n")
            found = True
        else:
            f.write(line)
    if not found:
        f.write(f"KITE_ACCESS_TOKEN={access_token}\n")

print("Saved access token to access_token.txt and .env")

kite.set_access_token(access_token)
print("Authentication Success")