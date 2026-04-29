import base64
import os
import time
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

TWOCAPTCHA_API_KEY = os.getenv("TWOCAPTCHA_API_KEY")


def _solve_2captcha(img_bytes: bytes, regsense: int = 0) -> str:
    img_b64 = base64.b64encode(img_bytes).decode()
    resp = requests.post("http://2captcha.com/in.php", data={
        "key": TWOCAPTCHA_API_KEY,
        "method": "base64",
        "body": img_b64,
        "json": 1,
        "regsense": regsense,
    })
    data = resp.json()
    if data.get("status") != 1:
        raise Exception(f"2captcha submit error: {data}")

    captcha_id = data["request"]
    print(f"  [2captcha] Aguardando resolução (id={captcha_id})...")

    for _ in range(24):
        time.sleep(5)
        res = requests.get("http://2captcha.com/res.php", params={
            "key": TWOCAPTCHA_API_KEY,
            "action": "get",
            "id": captcha_id,
            "json": 1,
        })
        data = res.json()
        if data.get("status") == 1:
            return data["request"]
        if data.get("request") != "CAPCHA_NOT_READY":
            raise Exception(f"2captcha error: {data}")

    raise Exception("2captcha timeout após 2 minutos")


async def resolve_captcha(page, base_url: str, selector: str = "img.imagem-captcha") -> str:
    el = page.locator(selector)
    await el.wait_for(state="visible", timeout=10_000)

    src = await el.get_attribute("src")
    captcha_url = base_url.rstrip("/") + "/" + src if not src.startswith("http") else src

    response = await page.request.get(captcha_url)
    img_bytes = await response.body()

    os.makedirs("debug_captchas", exist_ok=True)
    idx = len(os.listdir("debug_captchas"))
    with open(f"debug_captchas/captcha_{idx}.png", "wb") as f:
        f.write(img_bytes)


    return _solve_2captcha(img_bytes)
