#!/usr/bin/env python3
"""
Script de atualização automática dos dados de Repetitivos e IACs do STJ.
Extrai dados de https://processo.stj.jus.br/repetitivos/temas_repetitivos/

Usa Playwright (Chromium headless) para superar o JS challenge do F5 BIG-IP
que protege o site (cookies TS*).
"""
import re, json, os, sys, time, random
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE = "https://processo.stj.jus.br/repetitivos/temas_repetitivos/"

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")

# Domínios de analytics/tracking que podem bloquear o networkidle.
# Vamos abortar essas requisições para acelerar e evitar timeouts.
BLOCKED_HOSTS = (
    'google-analytics.com', 'googletagmanager.com', 'analytics.google.com',
    'google.com/ads', 'doubleclick.net', 'googleadservices.com',
    'hotjar.com', 'static.hotjar.com', 'script.hotjar.com',
    'analytics.stj.jus.br', 'matomo',
    'fonts.googleapis.com', 'fonts.gstatic.com',
)


def parse_page(html):
    themes = []
    blocks = html.split('containerDocumento')

    for block in blocks[1:]:
        t = {}

        m = re.search(r'dados_campo_processo\s+fonte_destaque[^>]*>\s*(\d+)', block)
        if not m:
            continue
        t['tema'] = m.group(1)

        pairs = re.findall(
            r'titulo_campo(?:_processo)?"[^>]*>(.*?)</div>\s*'
            r'<div[^>]*class="col-\d+\s+dados_campo(?:_processo)?[^"]*"[^>]*>(.*?)</div>',
            block, re.DOTALL
        )

        for label, value in pairs:
            cl = re.sub(r'<[^>]+>', '', label).strip()
            cv = re.sub(r'<[^>]+>', ' ', value).strip()
            cv = re.sub(r'\s+', ' ', cv)

            if cl.startswith('Tema Repetitivo'):
                continue
            if cl and cv:
                t[cl] = cv

        for field in ['Questão submetida a julgamento', 'Tese Firmada', 'Anotações NUGEPNAC',
                      'Delimitação do Julgado', 'Repercussão Geral', 'Situação do Tema']:
            if field not in t:
                pattern = re.escape(field) + r'.*?</div>\s*<div[^>]*>(.*?)</div>'
                m = re.search(pattern, block, re.DOTALL)
                if m:
                    val = re.sub(r'<[^>]+>', ' ', m.group(1)).strip()
                    val = re.sub(r'\s+', ' ', val)
                    if val:
                        t[field] = val

        if 'Situação' not in t and 'Situação do Tema' in t:
            t['Situação'] = t.pop('Situação do Tema')

        t['link'] = (
            f"https://processo.stj.jus.br/repetitivos/temas_repetitivos/"
            f"pesquisa.jsp?novaConsulta=true&tipo_pesquisa=T"
            f"&cod_tema_inicial={t['tema']}&cod_tema_final={t['tema']}"
        )

        themes.append(t)

    return themes


def _block_trackers(route, request):
    """Bloqueia recursos de analytics que atrapalham networkidle / podem dar 503."""
    url = request.url
    for host in BLOCKED_HOSTS:
        if host in url:
            return route.abort()
    return route.continue_()


def _try_goto(page, url, max_retries=3):
    """Faz goto com retries, usando 'domcontentloaded' (não 'networkidle')."""
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = page.goto(url, wait_until='domcontentloaded', timeout=60_000)
            status = resp.status if resp else None
            if status and status >= 400:
                print(f"  Tentativa {attempt}/{max_retries}: status {status}")
                page.wait_for_timeout(3000 + attempt * 2000)
                continue
            return resp
        except PWTimeout as e:
            last_err = e
            print(f"  Timeout tentativa {attempt}/{max_retries}")
            page.wait_for_timeout(3000 + attempt * 2000)
        except Exception as e:
            last_err = e
            print(f"  Erro tentativa {attempt}/{max_retries}: {e}")
            page.wait_for_timeout(3000 + attempt * 2000)
    if last_err:
        raise last_err
    return None


def fetch_all_themes():
    """Usa Chromium headless para superar o JS challenge do F5 e extrair HTML."""
    all_themes = {}
    page_size = 100

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=['--disable-blink-features=AutomationControlled', '--no-sandbox'],
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale='pt-BR',
            timezone_id='America/Sao_Paulo',
            viewport={'width': 1366, 'height': 900},
        )
        # Bloqueia analytics para evitar problemas
        context.route('**/*', _block_trackers)

        page = context.new_page()

        # Warmup: passa pelo JS challenge do F5
        print("Warmup: acessando home para resolver JS challenge do F5...", flush=True)
        _try_goto(page, BASE)
        # Dá tempo do F5 challenge JS rodar e setar cookies TS*
        page.wait_for_timeout(8000)

        cookies = context.cookies()
        ts_cookies = [c['name'] for c in cookies if c['name'].startswith('TS')]
        print(f"  Cookies F5/TS obtidos: {ts_cookies}", flush=True)
        if not ts_cookies:
            print("  AVISO: nenhum cookie TS* — possível que o challenge não tenha rodado.", flush=True)

        page_start = 1
        while True:
            url = (
                f"{BASE}pesquisa.jsp?novaConsulta=true&tipo_pesquisa=T"
                f"&l={page_size}&i={page_start}"
            )
            print(f"Fetching page starting at {page_start}...", flush=True)

            try:
                resp = _try_goto(page, url)
                if resp and resp.status >= 400:
                    print(f"  Status {resp.status} após retries, abortando loop.", flush=True)
                    break
            except Exception as e:
                print(f"  Falha definitiva: {e}", flush=True)
                break

            # Espera o conteúdo dinâmico aparecer (se houver)
            try:
                page.wait_for_selector('text=Tema Repetitivo', timeout=15_000)
            except PWTimeout:
                print("  AVISO: seletor 'Tema Repetitivo' não apareceu em 15s.", flush=True)

            html = page.content()
            themes = parse_page(html)

            if not themes:
                print(f"  No themes found, stopping.", flush=True)
                # debug: imprime tamanho do HTML
                print(f"  HTML size: {len(html)} bytes", flush=True)
                break

            for t in themes:
                all_themes[t['tema']] = t

            print(f"  Found {len(themes)} themes (total unique: {len(all_themes)})", flush=True)

            if len(themes) < page_size:
                break

            page_start += page_size
            time.sleep(0.8 + random.uniform(0, 0.6))

        browser.close()

    return all_themes


def main():
    all_themes = fetch_all_themes()
    result = sorted(all_themes.values(), key=lambda x: int(x['tema']))

    script_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.join(script_dir, 'dados.json')

    if len(result) < 100:
        print(f"\nABORTADO: Apenas {len(result)} temas extraídos (mínimo: 100).", flush=True)
        print("Os dados existentes NÃO foram alterados.", flush=True)
        sys.exit(1)

    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\nTotal: {len(result)} temas salvos em {out_path}", flush=True)
    print(f"Com tese: {sum(1 for t in result if t.get('Tese Firmada', '') not in ('', '-'))}", flush=True)

    generate_html(result, script_dir)

    return len(result)


def generate_html(data, base_dir):
    """Regenerate index.html with updated data."""
    html_path = os.path.join(base_dir, 'index.html')

    if not os.path.exists(html_path):
        print("index.html not found, skipping HTML generation", flush=True)
        return

    with open(html_path, 'r', encoding='utf-8') as f:
        html = f.read()

    data_json = json.dumps(data, ensure_ascii=False)

    new_html = re.sub(
        r'const D=\[.*?\];',
        f'const D={data_json};',
        html,
        count=1,
        flags=re.DOTALL
    )

    if new_html != html:
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(new_html)
        print(f"index.html updated ({len(new_html):,} bytes)", flush=True)
    else:
        print("Warning: Could not update data in index.html", flush=True)


if __name__ == '__main__':
    main()
