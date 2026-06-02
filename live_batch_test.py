import asyncio

from batch_autosearch import run_batch_autosearch


async def progress(message: str):
    print(message, flush=True)


async def main():
    result = await run_batch_autosearch(
        "batch_search_test_input.xlsx",
        progress_cb=progress,
        headless=False,
    )
    print("RESULTS", result.get("total_results"), flush=True)
    print("ERRORS", result.get("errors"), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
