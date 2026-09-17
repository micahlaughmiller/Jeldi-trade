import os
from dotenv import load_dotenv
from schwab.auth import client_from_manual_flow

# 1. Load variables from the .env file into environment
load_dotenv()

# 2. Grab keys securely from environment variables
APP_KEY = os.getenv("SCHWAB_APP_KEY")
APP_SECRET = os.getenv("SCHWAB_APP_SECRET")

# Must match your registered Schwab developer settings
CALLBACK_URL = "https://127.0.0.1:8080"
TOKEN_PATH = "token.json"

def main():
    if not APP_KEY or not APP_SECRET:
        raise ValueError("Missing SCHWAB_APP_KEY or SCHWAB_APP_SECRET in .env file!")

    print("Starting Schwab OAuth Listener on https://127.0.0.1:8080...")
    
    # Opens local port 8080, captures redirect code, writes token.json
    client = client_from_manual_flow(
        api_key=APP_KEY,
        app_secret=APP_SECRET,
        callback_url=CALLBACK_URL,
        token_path=TOKEN_PATH
    )
    
    print("\n Success! Tokens saved to token.json.")

if __name__ == "__main__":
    main()