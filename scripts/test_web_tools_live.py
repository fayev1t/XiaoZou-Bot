"""服务器端手动冒烟：真网络跑 websearch / webfetch 两个工具。

用法（在项目根 qqbot/ 下执行）：
    python scripts/test_web_tools_live.py "搜索关键词" [fetch_top_n]
    python scripts/test_web_tools_live.py --fetch https://example.com/

websearch 需要 .env（或环境）里配好 TAVILY_API_KEY；webfetch 零配置。
"""

import asyncio
import json
import sys
from pathlib import Path

# 脚本直跑时把项目根塞进 sys.path，让命名空间包 qqbot 可导入。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from qqbot.services.agent_loop.tools.webfetch import WebfetchTool  # noqa: E402
from qqbot.services.agent_loop.tools.websearch import WebsearchTool  # noqa: E402


async def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "--fetch":
        if len(args) < 2:
            print("用法: python scripts/test_web_tools_live.py --fetch <url>")
            return
        print(f"webfetch: {args[1]}")
        outcome = await WebfetchTool().run({"url": args[1]})
    else:
        query = args[0] if args else "python 3.13 新特性"
        fetch_top_n = int(args[1]) if len(args) > 1 else 0
        print(f"websearch: {query!r} fetch_top_n={fetch_top_n}")
        outcome = await WebsearchTool().run(
            {"query": query, "fetch_top_n": fetch_top_n}
        )

    print("ok:", outcome.ok)
    if outcome.ok:
        print(json.dumps(outcome.result, ensure_ascii=False, indent=2)[:6000])
    else:
        print(f"{outcome.error_kind}: {outcome.error_message}")


if __name__ == "__main__":
    asyncio.run(main())
