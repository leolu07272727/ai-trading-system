"""
PREREQ: IB Gateway must be running, logged into paper account,
        API enabled (port 4002, trusted 127.0.0.1, Read-Only OFF).
"""
import asyncio
from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv(Path(__file__).parent / ".env")

HOST = os.getenv("IBKR_HOST", "127.0.0.1")
PORT = int(os.getenv("IBKR_PORT", "4002"))

async def main():
    from ib_async import IB
    ib = IB()
    print(f"Connecting to {HOST}:{PORT} clientId=99 ...")
    await ib.connectAsync(HOST, PORT, clientId=99)
    print(f"isConnected : {ib.isConnected()}")
    accounts = ib.managedAccounts()
    print(f"managedAccounts: {accounts}")
    if not accounts:
        print("WARNING: no managed accounts returned")
    else:
        for acc in accounts:
            if acc.startswith("DU"):
                print(f"  ✓ Paper account confirmed: {acc}")
            else:
                print(f"  ⚠ Not a paper (DU) account: {acc}")
    ib.disconnect()
    print("Disconnected.")

if __name__ == "__main__":
    asyncio.run(main())
