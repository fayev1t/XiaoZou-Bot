import asyncio
import httpx
import json
import os
import sys

# 尝试加载环境变量
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

async def test_searxng(query: str):
    # 从环境变量获取基准地址，如果没有则使用默认值
    base_url = os.getenv("SEARXNG_BASE_URL", "http://39.106.1.193:7505")
    print(f"正在测试 SearXNG 服务: {base_url}")
    print(f"搜索关键词: {query}")
    
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        try:
            url = f"{base_url.rstrip('/')}/search"
            params = {
                "q": query,
                "format": "json",
                "categories": "general",
                "language": "zh-CN",
            }
            
            response = await client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
            
            results = payload.get("results", [])
            print(f"\n--- 响应摘要 ---")
            print(f"返回结果总数: {len(results)}")
            
            if results:
                print(f"\n--- 前 3 条结果示例 ---")
                for index, item in enumerate(results[:3]):
                    print(f"结果 {index + 1}:")
                    print(f"  标题: {item.get('title', 'N/A')}")
                    print(f"  URL: {item.get('url', 'N/A')}")
                    print(f"  摘要: {item.get('content', 'N/A')[:100]}...")
                    print("-" * 20)
            
            # 提供保存到文件的选项，防止终端输出过多
            output_file = "searxng_response.json"
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            print(f"\n完整响应内容已保存至: {output_file}")
            
        except httpx.HTTPStatusError as e:
            print(f"HTTP 错误: {e.response.status_code}")
            print(f"详细信息: {e.response.text}")
        except Exception as e:
            print(f"发生意外错误: {type(e).__name__}: {str(e)}")

if __name__ == "__main__":
    search_query = sys.argv[1] if len(sys.argv) > 1 else "Python 异步编程"
    asyncio.run(test_searxng(search_query))
