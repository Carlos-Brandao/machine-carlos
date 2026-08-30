import base64
import os
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from dotenv import load_dotenv

from machine_admin.secret_store import get_runtime_secret
from services.proxy import HttpProxy

load_dotenv(Path(__file__).parent.parent / ".env")

TWOCAPTCHA_SUBMIT_URL = "https://2captcha.com/in.php"
TWOCAPTCHA_RESULT_URL = "https://2captcha.com/res.php"
TWOCAPTCHA_HTTP_TIMEOUT = 20


class CaptchaError(RuntimeError):
    """Falha configurável ou transitória ao resolver um captcha."""


@dataclass(frozen=True, slots=True)
class TurnstileSolution:
    token: str
    user_agent: str | None = None


def resolve_turnstile(
    page,
    selector: str = ".cf-turnstile",
    *,
    proxy: HttpProxy | None = None,
) -> TurnstileSolution:
    """Resolve um Cloudflare Turnstile visível usando o cofre do sistema.

    A chamada é síncrona porque os adapters transacionais usam Playwright sync.
    O token é devolvido ao adapter, que conhece o formulário correto e faz a
    submissão. Nenhum segredo ou token é escrito em log/arquivo.
    """
    api_key = get_runtime_secret("TWOCAPTCHA_API_KEY")
    if not api_key:
        raise CaptchaError("TWOCAPTCHA_API_KEY não configurada.")
    widget = page.locator(selector).first
    # O JSF redesenha e pode ocultar o contêiner depois de um POST sem token,
    # mas o sitekey permanece disponível no elemento anexado ao formulário.
    widget.wait_for(state="attached", timeout=10_000)
    sitekey = str(widget.get_attribute("data-sitekey") or "").strip()
    if not sitekey:
        raise CaptchaError("O Turnstile não informou o sitekey.")
    browser_user_agent = str(page.evaluate("navigator.userAgent") or "").strip()
    submit_data = {
        "key": api_key,
        "method": "turnstile",
        "sitekey": sitekey,
        "pageurl": page.url,
        "userAgent": browser_user_agent,
        "json": 1,
    }
    if proxy is not None:
        # O SAFE valida o IP do token. O solver e o Chromium precisam sair
        # pelo mesmo proxy durante todo o fluxo.
        submit_data.update(
            {
                "proxytype": "HTTP",
                "proxy": proxy.twocaptcha_value(),
            }
        )
    response = requests.post(
        TWOCAPTCHA_SUBMIT_URL,
        data=submit_data,
        timeout=TWOCAPTCHA_HTTP_TIMEOUT,
    )
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError as exc:
        raise CaptchaError("O 2Captcha retornou uma resposta inválida.") from exc
    if data.get("status") != 1:
        raise CaptchaError(
            f"Falha ao enviar Turnstile: {data.get('request', 'erro desconhecido')}."
        )
    captcha_id = data["request"]
    for _ in range(30):
        time.sleep(5)
        result = requests.get(
            TWOCAPTCHA_RESULT_URL,
            params={
                "key": api_key,
                "action": "get",
                "id": captcha_id,
                "json": 1,
            },
            timeout=TWOCAPTCHA_HTTP_TIMEOUT,
        )
        result.raise_for_status()
        try:
            data = result.json()
        except ValueError as exc:
            raise CaptchaError("O 2Captcha retornou uma resposta inválida.") from exc
        if data.get("status") == 1:
            token = str(data.get("request") or "").strip()
            if not token:
                raise CaptchaError("O 2Captcha retornou um token vazio.")
            returned_user_agent = str(data.get("useragent") or "").strip()
            return TurnstileSolution(
                token=token,
                user_agent=returned_user_agent or browser_user_agent or None,
            )
        if data.get("request") != "CAPCHA_NOT_READY":
            raise CaptchaError(
                f"Falha ao resolver Turnstile: {data.get('request', 'erro desconhecido')}."
            )
    raise CaptchaError("Tempo limite do Turnstile excedido após 150 segundos.")


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
