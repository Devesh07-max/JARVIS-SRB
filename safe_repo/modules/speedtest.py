from safe_repo.core.speedtest_utils import run_speedtest

async def run_admin_speedtest(timeout=90):
    return await run_speedtest(timeout=timeout)
