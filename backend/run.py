"""Run Math Checker V5 backend: python run.py [port]"""

import sys

import uvicorn

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8642
    uvicorn.run("app.main:app", host="127.0.0.1", port=port, log_level="info")
