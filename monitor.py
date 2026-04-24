import asyncio
import json
import os
import re
import subprocess
import webbrowser
from collections import defaultdict
from datetime import datetime

import httpx
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

LINE_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_USER = os.getenv("LINE_USER_ID")
INTERVAL = int(os.getenv("CHECK_INTERVAL", "10"))
SUBS_FILE = "subscriptions.json"

_PRICE_RE = re.compile(r"(\d{4,5})$")


def load_subs() -> list[dict]:
    with open(SUBS_FILE, encoding="utf-8") as f:
        return json.load(f)


def notify_all(title: str, body: str):
    push_line(f"{title}\n\n{body}" if body else title)
    safe_title = title.replace('"', '\\"')
    safe_body = body.replace('"', '\\"')
    subprocess.run(
        ["osascript", "-e", f'display notification "{safe_body}" with title "{safe_title}"'],
        capture_output=True,
    )


def open_ticket_page(url: str):
    try:
        if webbrowser.open(url, new=2):
            print("       → 已自動開啟瀏覽器")
        else:
            print("       → 自動開瀏覽器失敗（請手動點連結）")
    except Exception as e:
        print(f"       → 自動開瀏覽器例外：{e}")


def push_line(text: str):
    try:
        r = httpx.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": f"Bearer {LINE_TOKEN}"},
            json={"to": LINE_USER, "messages": [{"type": "text", "text": text}]},
            timeout=10,
        )
        if r.status_code != 200:
            print(f"  [LINE 發送失敗] {r.status_code}: {r.text}")
    except Exception as e:
        print(f"  [LINE 例外] {e}")


def parse_price(area_name: str) -> tuple[str, str]:
    """Return (zone_name, formatted_price) from e.g. 'A1區6880'."""
    m = _PRICE_RE.search(area_name)
    if m:
        return _PRICE_RE.sub("", area_name).strip(), f"NT${int(m.group(1)):,}"
    return area_name, "未知價格"


async def scrape_area(url: str, page) -> dict[str, str]:
    await page.goto(url, wait_until="networkidle", timeout=30_000)
    await page.wait_for_timeout(2_000)

    results = {}
    for item in await page.query_selector_all("ul.area-list li"):
        font = await item.query_selector("font")
        if font:
            color = (await font.get_attribute("color") or "").upper()
            text = (await font.inner_text()).strip()
        else:
            color = ""
            text = (await item.inner_text()).strip()
        if not text:
            continue
        sold_out = color == "#AAAAAA" or "已售完" in text
        area_name = text.replace("已售完", "").strip()
        if area_name:
            status = "已售完" if sold_out else "有票"
            results[area_name] = status
            if not sold_out:
                html = await item.evaluate("el => el.outerHTML")
                print(f"  [有票 HTML] {html}")

    return results


async def main():
    subs = load_subs()
    prev: dict[str, dict] = {}
    scan = 0
    monitored_labels = [sub.get("label", sub["url"]) for sub in subs]
    monitored_text = "\n".join(f"{idx}. {name}" for idx, name in enumerate(monitored_labels, start=1))

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="zh-TW",
        )
        page = await ctx.new_page()
        notify_all(
            "🟢 拓元監控已啟動",
            f"每 {INTERVAL} 秒掃描一次，有票時立即通知\n\n目前監控場次：\n{monitored_text}",
        )
        print(f"🤖 拓元票況監控啟動（每 {INTERVAL} 秒掃描）\n按 Ctrl+C 停止\n")

        try:
            while True:
                scan += 1
                print(f"━━━━━━━━━━  #{scan}  {datetime.now().strftime('%H:%M:%S')}  ━━━━━━━━━━")

                for sub in subs:
                    url = sub["url"]
                    label = sub.get("label", url)
                    try:
                        current = await scrape_area(url, page)
                        old = prev.get(url, {})

                        available, newly_available, sold = [], [], 0
                        for area, status in current.items():
                            if status == "有票":
                                available.append(area)
                                if old.get(area) != "有票":
                                    newly_available.append(area)
                            else:
                                sold += 1

                        if available:
                            print(f"  ✅ 有票！  {label}  →  {len(available)} 區可購")
                            for a in available:
                                print(f"       {a}")
                        else:
                            print(f"  ❌ 全售完  {label}  （共 {sold} 區）")

                        if newly_available and old:
                            by_price: dict[str, list[str]] = defaultdict(list)
                            for a in newly_available:
                                zone, price = parse_price(a)
                                by_price[price].append(zone)

                            areas_str = "\n".join(
                                f"💰 {p}：{'、'.join(zones)}"
                                for p, zones in sorted(by_price.items(), reverse=True)
                            )
                            notify_all(f"🎫 {label} 有票！", f"{areas_str}\n\n立即購票：\n{url}")
                            open_ticket_page(url)
                            print("       → LINE + 桌面通知已發送")

                        prev[url] = current

                    except Exception as e:
                        print(f"  ⚠️  {label} 檢查失敗：{e}")

                print()
                await asyncio.sleep(INTERVAL)

        except asyncio.CancelledError:
            pass
        finally:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            notify_all("🔴 拓元監控已停止", now)
            print("監控已停止")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
