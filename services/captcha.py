import base64
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

from machine_admin.secret_store import get_runtime_secret

load_dotenv(Path(__file__).parent.parent / ".env")

TWOCAPTCHA_SUBMIT_URL = "https://2captcha.com/in.php"
TWOCAPTCHA_RESULT_URL = "https://2captcha.com/res.php"
TWOCAPTCHA_HTTP_TIMEOUT = 20


class CaptchaError(RuntimeError):
    """Falha configurável ou transitória ao resolver um captcha."""


def _solve_2captcha(img_bytes: bytes, regsense: int = 0) -> str:
    api_key = get_runtime_secret("TWOCAPTCHA_API_KEY")
    if not api_key:
        raise CaptchaError("TWOCAPTCHA_API_KEY não configurada.")
    if not img_bytes:
        raise CaptchaError("A imagem do captcha está vazia.")
    img_b64 = base64.b64encode(img_bytes).decode()
    resp = requests.post(TWOCAPTCHA_SUBMIT_URL, data={
        "key": api_key,
        "method": "base64",
        "body": img_b64,
        "json": 1,
        "regsense": regsense,
    }, timeout=TWOCAPTCHA_HTTP_TIMEOUT)
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError as exc:
        raise CaptchaError("O 2Captcha retornou uma resposta inválida.") from exc
    if data.get("status") != 1:
        raise CaptchaError(f"Falha ao enviar captcha: {data.get('request', 'erro desconhecido')}.")

    captcha_id = data["request"]
    print(f"  [2captcha] Aguardando resolução (id={captcha_id})...")

    for _ in range(24):
        time.sleep(5)
        res = requests.get(TWOCAPTCHA_RESULT_URL, params={
            "key": api_key,
            "action": "get",
            "id": captcha_id,
            "json": 1,
        }, timeout=TWOCAPTCHA_HTTP_TIMEOUT)
        res.raise_for_status()
        try:
            data = res.json()
        except ValueError as exc:
            raise CaptchaError("O 2Captcha retornou uma resposta inválida.") from exc
        if data.get("status") == 1:
            answer = str(data["request"]).strip()
            if not answer:
                raise CaptchaError("O 2Captcha retornou uma resposta vazia.")
            return answer
        if data.get("request") != "CAPCHA_NOT_READY":
            raise CaptchaError(f"Falha ao resolver captcha: {data.get('request', 'erro desconhecido')}.")

    raise CaptchaError("Tempo limite do 2Captcha excedido após 2 minutos.")


async def resolve_captcha(page, base_url: str, selector: str = "img.imagem-captcha") -> str:
    el = page.locator(selector)
    await el.wait_for(state="visible", timeout=10_000)

    src = await el.get_attribute("src")
    captcha_url = base_url.rstrip("/") + "/" + src if not src.startswith("http") else src

    response = await page.request.get(captcha_url)
    img_bytes = await response.body()

    if os.getenv("CAPTCHA_DEBUG", "0") == "1":
        os.makedirs("debug_captchas", exist_ok=True)
        idx = len(os.listdir("debug_captchas"))
        with open(f"debug_captchas/captcha_{idx}.png", "wb") as file:
            file.write(img_bytes)

    return _solve_2captcha(img_bytes)
