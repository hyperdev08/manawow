# ManaWoW

ManaWoW is an experimental, asyncio-based Python private server for **World of Warcraft 3.3.5a (build 12340)**.

> **Disclaimer:** This project is for educational and reverse-engineering research purposes only. Running private servers may violate Blizzard's Terms of Service depending on your jurisdiction. Use at your own risk.

## Getting started

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. (Optional) Create your local config — without it the example config is used
cp config/config.example.yaml config/config.yaml

# 3. Initialize the databases (creates data/auth.db, updates data/world.db)
python tools/init_db.py

# 4. Create a test account
python tools/create_test_account.py TEST TEST

# 5. (Optional) Download extracted terrain/VMaps/MMaps for real LOS + pathfinding
python tools/fetch_client_data.py --preset starter

# 6. Start the servers
python run_servers.py
#    Or run auth and world separately:
#    python run_auth.py
#    python run_world.py
```

Log in with username `TEST` and password `TEST`.

## Running the tests

```bash
pip install -r requirements-dev.txt
pytest                 # unit tests + end-to-end protocol self-test
pytest -m "not slow"   # unit tests only
```

CI runs the same suite on GitHub Actions (Python 3.11 and 3.13) for every push to `main` and every pull request.

Read [DOCUMENTATION.md](DOCUMENTATION.md) for:
- Full feature list
- Importing the CMaNGOS 3.3.5a world database
- Client setup (realmlist, nameplate CVars, addons)
- Verifying the protocol
- Project layout

## Requirements

- Python 3.11+
- `pyyaml`