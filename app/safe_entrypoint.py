import asyncio

from app.runtime_bootstrap import run_production


if __name__ == "__main__":
    asyncio.run(run_production())
