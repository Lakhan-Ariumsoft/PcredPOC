import asyncio
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from app.services.client_factory import get_llm_adapter

async def main():
    adapter = get_llm_adapter()
    print("Testing chat completion with adapter...")
    try:
        response = await adapter.chat(
            messages=[
                {"role": "system", "content": "You are a financial analyst."},
                {"role": "user", "content": "Hello! Please extract operating months."},
            ],
            temperature=0.0
        )
        import sys
        sys.stdout.reconfigure(encoding='utf-8')
        print("Success! Response:")
        print(repr(response))
    except Exception as e:
        print("Failed with exception:", e)

if __name__ == "__main__":
    asyncio.run(main())
