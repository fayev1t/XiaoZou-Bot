import asyncio
import httpx
import json
import os
import sys

# 尝试加载环境配置
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

async def test_crawl4ai(url: str):
    # 从 .env 获取地址，默认为 127.0.0.1:7506
    base_url = os.getenv("CRAWL4AI_BASE_URL", "http://127.0.0.1:7506")
    crawl_endpoint = f"{base_url.rstrip('/')}/crawl"
    
    print(f"📡 正在连接 Crawl4AI 服务: {crawl_endpoint}")
    print(f"🔗 尝试抓取 URL: {url}\n")

    payload = {
        "urls": [url],
        "browser_config": {"type": "BrowserConfig", "params": {"headless": True}},
        "crawler_config": {"type": "CrawlerRunConfig", "params": {"stream": False}},
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(crawl_endpoint, json=payload)
            response.raise_for_status()
            data = response.json()
            
            # 解析 Crawl4AI 返回的复杂嵌套结构
            results = data.get("results") or data.get("result") or data.get("data", {}).get("results")
            
            if not results:
                print("❌ 服务返回了成功响应，但未找到抓取结果。")
                print("原始响应:", json.dumps(data, indent=2, ensure_ascii=False))
                return

            # 如果是列表取第一个
            if isinstance(results, list):
                result = results[0]
            else:
                result = results

            print("✅ 抓取成功！结果摘要：")
            print(f"  - 标题: {result.get('metadata', {}).get('title') or result.get('title', 'N/A')}")
            print(f"  - URL: {result.get('url', 'N/A')}")
            
            content = result.get('markdown') or result.get('text') or result.get('cleaned_html', '')
            print(f"  - 内容预览 (前 200 字):\n{content[:200]}...")
            
            # 保存到本地方便查看全量
            with open("crawl4ai_result.json", "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            print(f"\n📦 完整抓取载荷已保存至: crawl4ai_result.json")

        except httpx.ConnectError:
            print(f"❌ 无法连接到服务，请确保容器正在运行且端口 {base_url} 可访问。")
        except httpx.HTTPStatusError as e:
            print(f"❌ 服务返回错误状态码: {e.response.status_code}")
            print(f"详情: {e.response.text}")
        except Exception as e:
            print(f"⚠️ 发生意外错误: {str(e)}")

if __name__ == "__main__":
    target_url = sys.argv[1] if len(sys.argv) > 1 else "https://www.baidu.com"
    asyncio.run(test_crawl4ai(target_url))
